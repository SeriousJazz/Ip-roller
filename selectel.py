import time
import requests

# === НАСТРОЙКИ АВТОРИЗАЦИИ SELECTEL ===
ACCOUNT_ID = "726879"  # Твой 6-значный номер аккаунта
PROJECT_ID = "f14fcb45c22f46d5a7824b507fa21ca1"  # UUID проекта в Облачной платформе
USERNAME = "Shea"  # Логин сервисного пользователя
PASSWORD = "^gUeSadPL.oFl#5:FHEr"  # Пароль сервисного пользователя

# === НУЖНЫЕ ПРЕФИКСЫ IP ===
TARGET_PREFIXES = (
    "81.163.22.",
    "81.163.23.",
    "87.228.32.",
    "87.228.33.",
    "87.228.101.",
    "185.91.52.",
    "37.9.4.",
    "31.129.42.",
    "46.182.24.",
    "5.178.85.",
)

MAX_ATTEMPTS_PER_RUN = 200  # Общее число попыток за запуск
DELAY_BETWEEN_ATTEMPTS = 5  # Пауза между попытками (сек)

# Настройки Telegram (по желанию)
TG_BOT_TOKEN = "8876385443:AAFDbBNALp-N0AxlPVD_7EShHpBX0X_p7dU"
TG_CHAT_ID = "521789263"

AUTH_URL = "https://cloud.api.selcloud.ru/identity/v3/auth/tokens"


def send_tg_message(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10
        )
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")


def is_target_ip(ip_str):
    """Проверяет, начинается ли IP на любой из нужных префиксов."""
    return ip_str.startswith(TARGET_PREFIXES)


def get_keystone_session():
    """Авторизуется и достает ТОЛЬКО зоны ru-* из каталога Selectel."""
    payload = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "name": USERNAME,
                        "domain": {"name": str(ACCOUNT_ID)},
                        "password": PASSWORD,
                    }
                },
            },
            "scope": {"project": {"id": PROJECT_ID}},
        }
    }
    resp = requests.post(AUTH_URL, json=payload, timeout=20)
    resp.raise_for_status()

    token = resp.headers.get("X-Subject-Token")
    catalog = resp.json().get("token", {}).get("catalog", [])

    # Собираем только российские регионы (начинаются на "ru-")
    regions_map = {}
    for service in catalog:
        if service.get("type") == "network":
            for ep in service.get("endpoints", []):
                if ep.get("interface") == "public":
                    reg = ep.get("region_id", "")
                    url = ep.get("url", "").rstrip("/")
                    if reg.startswith("ru-") and url:
                        regions_map[reg] = url

    if not regions_map:
        raise RuntimeError(
            "Не найдено ни одного региона ru-* в каталоге Selectel."
        )

    return token, regions_map


def get_external_network_id(headers, network_url):
    url = f"{network_url}/v2.0/networks?router:external=true"
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    networks = resp.json().get("networks", [])
    if not networks:
        return None
    for net in networks:
        if net.get("name") == "external-network":
            return net["id"]
    return networks[0]["id"]


def list_floating_ips(headers, network_url):
    url = f"{network_url}/v2.0/floatingips"
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json().get("floatingips", [])


def create_floating_ip(headers, network_url, ext_net_id):
    url = f"{network_url}/v2.0/floatingips"
    payload = {"floatingip": {"floating_network_id": ext_net_id}}
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json().get("floatingip", {})


def delete_floating_ip(headers, network_url, fip_id):
    url = f"{network_url}/v2.0/floatingips/{fip_id}"
    for _ in range(3):
        resp = requests.delete(url, headers=headers, timeout=15)
        if resp.status_code in (204, 404):
            print("[-] IP удален.")
            return True
        time.sleep(3)
    print(f"[!] Не удалось сразу удалить {fip_id}: код {resp.status_code}")
    return False


def cleanup_region_ips(headers, network_url, region_name):
    """Удаляет все непривязанные нецелевые IP в конкретном регионе и возвращает их количество."""
    cleaned = 0
    try:
        for fip in list_floating_ips(headers, network_url):
            addr = fip.get("floating_ip_address", "")
            fip_id = fip.get("id", "")
            if not is_target_ip(addr) and not fip.get("port_id"):
                print(f"[*] [{region_name}] Чистим лишний IP: {addr}")
                if delete_floating_ip(headers, network_url, fip_id):
                    cleaned += 1
    except Exception as e:
        print(f"[!] [{region_name}] Ошибка при чистке: {e}")
    return cleaned


def main():
    print("Авторизация в Selectel и поиск зон ru-*...")
    token, regions_map = get_keystone_session()
    headers = {"X-Auth-Token": token, "Content-Type": "application/json"}

    print(
        f"Найдены российские зоны: {', '.join(sorted(regions_map.keys()))}"
    )

    active_regions = {}

    for reg, net_url in sorted(regions_map.items()):
        try:
            # 1. Проверяем, вдруг нужный IP уже выбит
            existing = list_floating_ips(headers, net_url)
            for fip in existing:
                ip_str = fip.get("floating_ip_address", "")
                if is_target_ip(ip_str):
                    print(
                        f"\n[+] УРА! Нужный IP уже есть в зоне {reg}: {ip_str}. Выходим!"
                    )
                    return

            # 2. Чистим хвосты от старых запусков
            cleanup_region_ips(headers, net_url, reg)

            # 3. Ищем ID внешней сети
            ext_net_id = get_external_network_id(headers, net_url)
            if ext_net_id:
                active_regions[reg] = {"url": net_url, "ext_net_id": ext_net_id}
                print(f"[OK] Зона {reg} подключена.")
            else:
                print(f"[-] В зоне {reg} нет внешней сети, пропускаем.")
        except Exception as e:
            print(f"[-] Зона {reg} недоступна ({e}), пропускаем.")

    region_names = list(active_regions.keys())
    if not region_names:
        print("[!] Нет ни одной доступной зоны ru-* для работы.")
        return

    print(f"\nНачинаем прокрутку по зонам: {', '.join(region_names)}")

    attempt = 1
    reg_index = 0

    while attempt <= MAX_ATTEMPTS_PER_RUN and region_names:
        current_reg = region_names[reg_index % len(region_names)]
        reg_info = active_regions[current_reg]
        net_url = reg_info["url"]
        ext_net_id = reg_info["ext_net_id"]

        print(
            f"\n--- Попытка {attempt}/{MAX_ATTEMPTS_PER_RUN} [Зона: {current_reg}] ---"
        )
        try:
            new_fip = create_floating_ip(headers, net_url, ext_net_id)
            ip_address = new_fip.get("floating_ip_address", "")
            fip_id = new_fip.get("id", "")

            print(f"[{current_reg}] Выпал IP: {ip_address}")

            if is_target_ip(ip_address):
                msg = f"🎯 ПОБЕДА! В Selectel (зона {current_reg}) выбит нужный IP: {ip_address}"
                print(f"\n{msg}")
                send_tg_message(msg)
                for r_name in region_names:
                    cleanup_region_ips(
                        headers, active_regions[r_name]["url"], r_name
                    )
                return
            else:
                print("Не подходит. Удаляем...")
                time.sleep(2)
                delete_floating_ip(headers, net_url, fip_id)
                attempt += 1
                reg_index += 1

        except requests.exceptions.HTTPError as err:
            code = err.response.status_code
            text = err.response.text
            print(f"[!] [{current_reg}] Ошибка API (Код {code}): {text}")

            if code == 401:
                print("[*] Обновляем токен авторизации...")
                token, _ = get_keystone_session()
                headers["X-Auth-Token"] = token
            elif code in (403, 409) and "Quota exceeded" in text:
                cleaned = cleanup_region_ips(headers, net_url, current_reg)
                if cleaned == 0:
                    # Если чистить нечего, а квота превышена — значит в этой зоне лимит 0 или все занято
                    print(
                        f"[*] В зоне {current_reg} нулевая квота на плавающие IP. Исключаем её из списка."
                    )
                    region_names.remove(current_reg)
                else:
                    reg_index += 1
                    time.sleep(5)
            elif code in (413, 429):
                print(
                    f"[*] Лимит запросов в зоне {current_reg}! Переключаемся на следующую и ждем 20 сек..."
                )
                cleanup_region_ips(headers, net_url, current_reg)
                reg_index += 1
                time.sleep(20)
            else:
                cleanup_region_ips(headers, net_url, current_reg)
                reg_index += 1
                time.sleep(10)
            continue

        except Exception as e:
            print(f"[!] Сбой сети: {e}. Ждем 15 сек...")
            time.sleep(15)
            continue

        time.sleep(DELAY_BETWEEN_ATTEMPTS)

    if not region_names:
        print(
            "\n[!] Во всех зонах ru-* закончилась квота на плавающие IP. Проверь раздел 'Квоты' в панели Selectel!"
        )
    else:
        print(f"\nВсе {MAX_ATTEMPTS_PER_RUN} попыток завершены.")


if __name__ == "__main__":
    main()
