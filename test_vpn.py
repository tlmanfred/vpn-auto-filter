import os
import re
import json
import base64
import urllib.request
import urllib.parse
import socket
import time
import asyncio
import aiohttp

# Источники подписок
SOURCES = [
    # Репозиторий igareck/vpn-configs-for-russia
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS_mobile.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_SS%2BAll_RUS.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
    
    # Репозиторий AvenCores/goida-vpn-configs
    "https://raw.githubusercontent.com/AvenCores/goida-vpn-configs/main/githubmirror/1.txt",
    "https://raw.githubusercontent.com/AvenCores/goida-vpn-configs/main/githubmirror/26.txt"
]

PRIORITY_COUNTRIES = ['NL', 'DE', 'FR']  # Нидерланды, Германия, Франция

def decode_base64_if_needed(content):
    try:
        return base64.b64decode(content).decode('utf-8')
    except Exception:
        return content

def fetch_configs():
    all_configs = []
    for url in SOURCES:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                raw_data = response.read().decode('utf-8', errors='ignore')
                text = decode_base64_if_needed(raw_data)
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                all_configs.extend(lines)
        except Exception as e:
            print(f"Ошибка загрузки {url}: {e}")
    
    # Удаление дубликатов
    unique_configs = list(set([c for c in all_configs if any(c.startswith(proto) for proto in ['vless://', 'ss://', 'trojan://', 'vmess://'])]))
    print(f"Всего найдено уникальных конфигов: {len(unique_configs)}")
    return unique_configs

def parse_host_port(config):
    try:
        if config.startswith('vless://') or config.startswith('trojan://'):
            part = config.split('@')[1]
            host_port = part.split('?')[0].split('/')[0]
            if ':' in host_port:
                host, port = host_port.rsplit(':', 1)
                return host, int(port)
    except Exception:
        pass
    return None, None

async def tcp_ping(host, port, timeout=2.0):
    start = time.time()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        latency = (time.time() - start) * 1000
        return round(latency, 1)
    except Exception:
        return None

async def get_geoip_batch(ips):
    # Пакетное определение стран через ip-api
    country_map = {}
    url = "http://ip-api.com/batch?fields=query,countryCode,country"
    payload = json.dumps([{"query": ip} for ip in ips]).encode('utf-8')
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=payload, timeout=10) as response:
                data = await response.json()
                for item in data:
                    country_map[item.get('query')] = (item.get('countryCode', 'UNKNOWN'), item.get('country', 'Unknown'))
    except Exception as e:
        print(f"GeoIP error: {e}")
    return country_map

async def filter_and_rank(configs):
    candidates = []
    ip_to_check = set()
    
    for cfg in configs:
        host, port = parse_host_port(cfg)
        if host and port:
            candidates.append({'config': cfg, 'host': host, 'port': port})
            ip_to_check.add(host)

    print("Проверка TCP-пига...")
    tasks = [tcp_ping(c['host'], c['port']) for c in candidates]
    pings = await asyncio.gather(*tasks)

    alive_candidates = []
    alive_ips = set()
    for item, ping in zip(candidates, pings):
        if ping is not None:
            item['ping'] = ping
            alive_candidates.append(item)
            alive_ips.add(item['host'])

    print(f"Живых узлов: {len(alive_candidates)}")

    # Определение стран
    geo_map = await get_geoip_batch(list(alive_ips))
    
    for item in alive_candidates:
        code, country = geo_map.get(item['host'], ('UNKNOWN', 'Unknown'))
        item['country_code'] = code
        item['country_name'] = country
        
        # Расчет приоритета:
        # Страны NL/DE/FR получают высший приоритет (weight 0), остальные (weight 1)
        priority_weight = 0 if code in PRIORITY_COUNTRIES else 1
        item['rank_score'] = (priority_weight, item['ping'])

    # Сортировка по приоритету страны, затем по минимальному пингу
    alive_candidates.sort(key=lambda x: x['rank_score'])

    top_20 = alive_candidates[:20]
    return top_20, len(configs), len(alive_candidates)

def send_telegram_message(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram не настроен.")
        return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    req = urllib.request.Request(url, data=payload.encode('utf-8'), headers={'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Ошибка отправки Telegram: {e}")

async def main():
    configs = fetch_configs()
    top_20, total_raw, total_alive = await filter_and_rank(configs)

    # Сохранение подписки в формате TXT и Base64
    out_configs = [item['config'] for item in top_20]
    sub_text = "\n".join(out_configs)
    
    with open("sub.txt", "w", encoding="utf-8") as f:
        f.write(sub_text)

    with open("sub_base64.txt", "w", encoding="utf-8") as f:
        f.write(base64.b64encode(sub_text.encode('utf-8')).decode('utf-8'))

    # Формирование отчета в Telegram
    country_stats = {}
    for item in top_20:
        cc = item['country_code']
        country_stats[cc] = country_stats.get(cc, 0) + 1

    stats_str = "\n".join([f"• `{cc}`: {count} шт." for cc, count in country_stats.items()])
    
    msg = (
        f"⚡️ **Обновление VPN-подписки готово!**\n\n"
        f"📊 **Статистика:**\n"
        f"• Обработано: `{total_raw}`\n"
        f"• Доступно: `{total_alive}`\n"
        f"• Отобрано в ТОП-20: `{len(top_20)}`\n\n"
        f"🌍 **Страны в ТОП-20:**\n{stats_str}\n\n"
        f"🚀 **Ссылка подписки для Throne и Podkop:**\n"
        f"`https://raw.githubusercontent.com/{os.environ.get('GITHUB_REPOSITORY')}/main/sub.txt`"
    )
    send_telegram_message(msg)

if __name__ == "__main__":
    asyncio.run(main())
