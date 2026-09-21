import os
import re
import json
import base64
import urllib.request
import urllib.parse
import time
import asyncio
import subprocess
import aiohttp

SOURCES = [
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS_mobile.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS.txt",
    "https://raw.githubusercontent.com/AvenCores/goida-vpn-configs/main/githubmirror/1.txt",
    "https://raw.githubusercontent.com/AvenCores/goida-vpn-configs/main/githubmirror/26.txt"
]

TEST_DOWNLOAD_URL = "http://cachefly.cachefly.net/10mb.test"

def decode_base64_if_needed(content):
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    decoded_lines = []
    for line in lines:
        if any(line.startswith(p) for p in ['vless://', 'ss://', 'trojan://']):
            decoded_lines.append(line)
        else:
            try:
                clean = line.replace('-', '+').replace('_', '/')
                pad = len(clean) % 4
                if pad: clean += '=' * (4 - pad)
                decoded = base64.b64decode(clean).decode('utf-8', errors='ignore')
                for d in decoded.splitlines():
                    if any(d.strip().startswith(p) for p in ['vless://', 'ss://', 'trojan://']):
                        decoded_lines.append(d.strip())
            except Exception:
                pass
    return decoded_lines if decoded_lines else lines

def fetch_configs():
    all_configs = []
    for url in SOURCES:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=12) as resp:
                all_configs.extend(decode_base64_if_needed(resp.read().decode('utf-8', errors='ignore')))
        except Exception as e:
            print(f"Ошибка загрузки {url}: {e}")
    
    valid = [c.strip('\ufeff\r\n ') for c in all_configs if any(c.strip().startswith(p) for p in ['vless://', 'trojan://'])]
    return list(set(valid))

def config_to_singbox(url_str, tag_name):
    try:
        parsed = urllib.parse.urlparse(url_str)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
        user_info = parsed.username or (parsed.netloc.split('@')[0] if '@' in parsed.netloc else None)
        params = urllib.parse.parse_qs(parsed.query)

        if not host or not port or not user_info:
            return None

        if scheme == 'vless':
            outbound = {
                "type": "vless",
                "tag": tag_name,
                "server": host,
                "server_port": int(port),
                "uuid": user_info
            }
            flow = params.get('flow', [None])[0]
            if flow: outbound["flow"] = flow

            security = params.get('security', ['none'])[0]
            if security in ['tls', 'reality']:
                sni = params.get('sni', [host])[0]
                tls_conf = {"enabled": True, "server_name": sni, "insecure": True}
                if security == 'reality':
                    pbk = params.get('pbk', [''])[0]
                    if not pbk:
                        return None
                    tls_conf["reality"] = {
                        "enabled": True,
                        "public_key": pbk,
                        "short_id": params.get('sid', [''])[0]
                    }
                outbound["tls"] = tls_conf

        elif scheme == 'trojan':
            outbound = {
                "type": "trojan",
                "tag": tag_name,
                "server": host,
                "server_port": int(port),
                "password": user_info,
                "tls": {
                    "enabled": True,
                    "server_name": params.get('sni', [host])[0],
                    "insecure": True
                }
            }
        else:
            return None

        transport = params.get('type', ['tcp'])[0]
        if transport == 'ws':
            outbound["transport"] = {
                "type": "ws",
                "path": params.get('path', ['/'])[0],
                "headers": {"Host": params.get('host', [host])[0]}
            }
        elif transport == 'grpc':
            outbound["transport"] = {
                "type": "grpc",
                "service_name": params.get('serviceName', [''])[0]
            }

        return outbound
    except Exception:
        return None

def measure_speed_via_curl():
    """Замеряет скорость скачивания через локальный прокси 127.0.0.1:1080 утилитой curl"""
    cmd = [
        "curl",
        "-s",
        "-o", "/dev/null",
        "-w", "%{speed_download}",
        "--max-time", "3",
        "-x", "http://127.0.0.1:1080",
        TEST_DOWNLOAD_URL
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        speed_bytes_sec = float(res.stdout.strip())
        speed_mbps = (speed_bytes_sec * 8) / 1_000_000
        return round(speed_mbps, 2)
    except Exception:
        return 0.0

async def run_full_test(configs):
    node_outbounds = []
    config_map = {}
    
    for idx, cfg in enumerate(configs):
        tag = f"node-{idx}"
        sb_obj = config_to_singbox(cfg, tag)
        if sb_obj:
            node_outbounds.append(sb_obj)
            config_map[tag] = cfg

    print(f"Сконвертировано валидных узлов: {len(node_outbounds)}")
    if not node_outbounds:
        return []

    # -------------------------------------------------------------
    # ЭТАП 1: Быстрая фильтрация отклика (Clash API delay)
    # -------------------------------------------------------------
    test_config = {
        "log": {"level": "warn"},
        "experimental": {
            "clash_api": {
                "external_controller": "127.0.0.1:9090"
            }
        },
        "outbounds": node_outbounds
    }

    with open("temp_runner.json", "w", encoding="utf-8") as f:
        json.dump(test_config, f)

    proc = subprocess.Popen(["./sing-box", "run", "-c", "temp_runner.json"])
    await asyncio.sleep(2.5)

    delay_passed_nodes = []
    print("🔹 Этап 1: Проверка задержки и отклика (HTTP 204)...")
    async with aiohttp.ClientSession() as session:
        for tag, cfg in config_map.items():
            test_url = f"http://127.0.0.1:9090/proxies/{urllib.parse.quote(tag)}/delay?url=http://cp.cloudflare.com/generate_204&timeout=2000"
            try:
                async with session.get(test_url, timeout=2.5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        delay = data.get("delay", 0)
                        if delay > 0:
                            delay_passed_nodes.append({'config': cfg, 'delay': delay, 'tag': tag, 'sb_obj': config_to_singbox(cfg, 'main-out')})
            except Exception:
                pass

    proc.terminate()
    proc.wait()

    print(f"Ответило на запрос отклика: {len(delay_passed_nodes)} узлов.")
    if not delay_passed_nodes:
        return []

    delay_passed_nodes.sort(key=lambda x: x['delay'])
    candidates = delay_passed_nodes[:15]  # Берем ТОП-15 лучших по задержке для измерения скорости

    # -------------------------------------------------------------
    # ЭТАП 2: Поочередный замер скорости через изолированный sing-box + curl
    # -------------------------------------------------------------
    print("🔹 Этап 2: Поочередное тестирование скорости скачивания (3 сек на узел)...")
    final_nodes = []

    for i, item in enumerate(candidates, 1):
        # Формируем индивидуальный конфиг под 1 узел
        single_config = {
            "log": {"level": "warn"},
            "inbounds": [
                {
                    "type": "mixed",
                    "tag": "mixed-in",
                    "listen": "127.0.0.1",
                    "listen_port": 1080
                }
            ],
            "outbounds": [item['sb_obj']]
        }

        with open("temp_single.json", "w", encoding="utf-8") as f:
            json.dump(single_config, f)

        # Запускаем локальный sing-box под этот конкретный узел
        single_proc = subprocess.Popen(["./sing-box", "run", "-c", "temp_single.json"])
        await asyncio.sleep(1.0)  # Даем 1 сек на старт

        # Замеряем скорость
        speed_mbps = measure_speed_via_curl()

        single_proc.terminate()
        single_proc.wait()

        if speed_mbps > 0.1:
            item['speed_mbps'] = speed_mbps
            final_nodes.append(item)
            print(f"  [{i}/15] ✅ {item['tag']}: Скорость = {speed_mbps} Мбит/с | Пинг = {item['delay']} ms")
        else:
            print(f"  [{i}/15] ❌ {item['tag']}: Не удалось выкачать файл (0 Мбит/с)")

    # Если никто не прошёл тест скорости, возвращаем первые 10 узлов по пингу (чтобы файлы не оставались пустыми)
    if not final_nodes:
        print("⚠️ Ни один узел не показал высокую скорость. Записываем лучшие по пингу.")
        for item in candidates[:10]:
            item['speed_mbps'] = 0.0
            final_nodes.append(item)

    final_nodes.sort(key=lambda x: x['speed_mbps'], reverse=True)
    return final_nodes

def send_telegram_message(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id: return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    req = urllib.request.Request(url, data=payload.encode('utf-8'), headers={'Content-Type': 'application/json'})
    try: urllib.request.urlopen(req)
    except Exception: pass

async def main():
    raw_configs = fetch_configs()
    print(f"Загружено уникальных конфигураций: {len(raw_configs)}")
    
    tested_nodes = await run_full_test(raw_configs)
    
    top_nodes = tested_nodes[:20]
    print(f"Итого отобрано узлов: {len(top_nodes)}")

    out_configs = [item['config'] for item in top_nodes]
    with open("sub.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(out_configs))

    with open("sub_base64.txt", "w", encoding="utf-8") as f:
        f.write(base64.b64encode("\n".join(out_configs).encode('utf-8')).decode('utf-8'))

    # Формируем nodes.json
    sb_outbounds = []
    tags = []
    for idx, item in enumerate(top_nodes):
        tag = f"node-{idx+1}-{item['speed_mbps']}Mbps"
        sb_obj = config_to_singbox(item['config'], tag)
        if sb_obj:
            sb_outbounds.append(sb_obj)
            tags.append(tag)

    if tags:
        sb_outbounds.append({
            "type": "urltest",
            "tag": "auto-outbound",
            "outbounds": tags,
            "url": "http://cp.cloudflare.com/generate_204",
            "interval": "5m"
        })

    with open("nodes.json", "w", encoding="utf-8") as f:
        json.dump({"outbounds": sb_outbounds}, f, indent=2, ensure_ascii=False)

    top_stats_str = "\n".join([f"• `{item['speed_mbps']} Мбит/с` (отклик {item['delay']} ms)" for item in top_nodes[:5]]) if top_nodes else "• Нет доступных серверов"

    repo_name = os.environ.get('GITHUB_REPOSITORY', 'tlmanfred/vpn-auto-filter')
    msg = (
        f"🚀 **Тестирование скорости завершено!**\n\n"
        f"📊 **Результаты:**\n"
        f"• Всего обработано: `{len(raw_configs)}`\n"
        f"• Отобрано в подписку: `{len(top_nodes)}`\n\n"
        f"🏆 **ТОП-5 по скорости скачивания:**\n{top_stats_str}\n\n"
        f"🔗 **Ссылка подписки:**\n"
        f"`https://raw.githubusercontent.com/{repo_name}/main/sub.txt`"
    )
    send_telegram_message(msg)

if __name__ == "__main__":
    asyncio.run(main())
