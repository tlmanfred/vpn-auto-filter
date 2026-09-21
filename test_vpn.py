import os
import re
import json
import base64
import urllib.request
import urllib.parse
import time
import asyncio
import aiohttp

# Источники подписок
SOURCES = [
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS_mobile.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_SS%2BAll_RUS.txt",
    "https://raw.githubusercontent.com/AvenCores/goida-vpn-configs/main/githubmirror/1.txt",
    "https://raw.githubusercontent.com/AvenCores/goida-vpn-configs/main/githubmirror/26.txt"
]

PRIORITY_COUNTRIES = ['NL', 'DE', 'FR']  # Нидерланды, Германия, Франция

def decode_base64_if_needed(content):
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    decoded_lines = []
    
    for line in lines:
        if any(line.startswith(p) for p in ['vless://', 'ss://', 'trojan://', 'vmess://', 'hysteria2://', 'hy2://']):
            decoded_lines.append(line)
        else:
            try:
                clean_line = line.replace('-', '+').replace('_', '/')
                missing_padding = len(clean_line) % 4
                if missing_padding:
                    clean_line += '=' * (4 - missing_padding)
                decoded = base64.b64decode(clean_line).decode('utf-8', errors='ignore')
                for d_line in decoded.splitlines():
                    d_clean = d_line.strip()
                    if any(d_clean.startswith(p) for p in ['vless://', 'ss://', 'trojan://', 'vmess://', 'hysteria2://', 'hy2://']):
                        decoded_lines.append(d_clean)
            except Exception:
                pass
                
    return decoded_lines if decoded_lines else lines

def fetch_configs():
    all_configs = []
    for url in SOURCES:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=12) as response:
                raw_data = response.read().decode('utf-8', errors='ignore')
                lines = decode_base64_if_needed(raw_data)
                all_configs.extend(lines)
                print(f"Загружено из {url}: {len(lines)} строк")
        except Exception as e:
            print(f"Ошибка загрузки {url}: {e}")
    
    valid_configs = []
    for c in all_configs:
        c_clean = c.strip('\ufeff\r\n ')
        if any(c_clean.startswith(proto) for proto in ['vless://', 'ss://', 'trojan://', 'vmess://']):
            valid_configs.append(c_clean)
            
    unique_configs = list(set(valid_configs))
    print(f"Всего распознано уникальных конфигов: {len(unique_configs)}")
    return unique_configs

def parse_host_port(config):
    try:
        clean_cfg = config.split('#')[0].split('?')[0]
        
        if '@' in clean_cfg:
            target = clean_cfg.split('@')[-1]
            if target.startswith('['):
                host = target.split(']')[0] + ']'
                port_str = target.split(']:')[1]
            else:
                if ':' in target:
                    host, port_str = target.rsplit(':', 1)
                else:
                    return None, None
            
            host = host.strip('/ ')
            port_digits = re.sub(r'\D', '', port_str)
            if port_digits:
                port = int(port_digits)
                if host and 1 <= port <= 65535:
                    return host, port
    except Exception:
        pass
    return None, None

async def tcp_ping(host, port, timeout=2.5):
    start = time.time()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        latency = (time.time() - start) * 1000
        return round(latency, 1)
    except Exception:
        return None

async def get_geoip_batch(hosts):
    country_map = {}
    if not hosts:
        return country_map
    
    hosts_list = list(hosts)
    chunk_size = 100
    
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(hosts_list), chunk_size):
            chunk = hosts_list[i:i + chunk_size]
            payload = json.dumps([{"query": h} for h in chunk]).encode('utf-8')
            try:
                async with session.post("http://ip-api.com/batch?fields=query,countryCode,country", data=payload, timeout=10) as response:
                    data = await response.json()
                    for item in data:
                        query_host = item.get('query')
                        cc = item.get('countryCode', 'UNKNOWN')
                        country = item.get('country', 'Unknown')
                        country_map[query_host] = (cc, country)
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

    print(f"К проверке TCP-пинга подготовлено узлов: {len(candidates)}")
    if not candidates:
        return [], len(configs), 0

    tasks = [tcp_ping(c['host'], c['port']) for c in candidates]
    pings = await asyncio.gather(*tasks)

    alive_candidates = []
    alive_ips = set()
    for item, ping in zip(candidates, pings):
        if ping is not None:
            item['ping'] = ping
            alive_candidates.append(item)
            alive_ips.add(item['host'])

    print(f"Живых узлов ответило на пинг: {len(alive_candidates)}")

    geo_map = await get_geoip_batch(alive_ips)
    
    for item in alive_candidates:
        code, country = geo_map.get(item['host'], ('UNKNOWN', 'Unknown'))
        item['country_code'] = code
        item['country_name'] = country
        
        priority_weight = 0 if code in PRIORITY_COUNTRIES else 1
        item['rank_score'] = (priority_weight, item['ping'])

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

    out_configs = [item['config'] for item in top_20]
    sub_text = "\n".join(out_configs)
    
    with open("sub.txt", "w", encoding="utf-8") as f:
        f.write(sub_text)

    with open("sub_base64.txt", "w", encoding="utf-8") as f:
        f.write(base64.b64encode(sub_text.encode('utf-8')).decode('utf-8'))

    country_stats = {}
    for item in top_20:
        cc = item['country_code']
        country_stats[cc] = country_stats.get(cc, 0) + 1

    stats_str = "\n".join([f"• `{cc}`: {count} шт." for cc, count in country_stats.items()]) if top_20 else "• Нет доступных узлов"
    
    repo_name = os.environ.get('GITHUB_REPOSITORY', 'tlmanfred/vpn-auto-filter')
    msg = (
        f"⚡️ **Обновление VPN-подписки готово!**\n\n"
        f"📊 **Статистика:**\n"
        f"• Обработано: `{total_raw}`\n"
        f"• Доступно: `{total_alive}`\n"
        f"• Отобрано в ТОП-20: `{len(top_20)}`\n\n"
        f"🌍 **Страны в ТОП-20:**\n{stats_str}\n\n"
        f"🚀 **Ссылка подписки для Throne и Podkop:**\n"
        f"`https://raw.githubusercontent.com/{repo_name}/main/sub.txt`"
    )
    send_telegram_message(msg)

if __name__ == "__main__":
    asyncio.run(main())
