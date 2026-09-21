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

TEST_DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes=10000000"  # Тестовый файл 10МБ
SPEED_TEST_DURATION = 3.0  # Замер задержки/скорости скачивания длится макс. 3 секунды на узел

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

def vless_to_singbox(url_str, tag_name):
    try:
        parsed = urllib.parse.urlparse(url_str)
        uuid = parsed.username
        host = parsed.hostname
        port = parsed.port
        params = urllib.parse.parse_qs(parsed.query)

        if not host or not port or not uuid:
            return None

        outbound = {
            "type": "vless",
            "tag": tag_name,
            "server": host,
            "server_port": int(port),
            "uuid": uuid,
        }

        flow = params.get('flow', [None])[0]
        if flow: outbound["flow"] = flow

        security = params.get('security', ['none'])[0]
        if security in ['tls', 'reality']:
            tls_conf = {"enabled": True, "server_name": params.get('sni', [host])[0], "insecure": True}
            if security == 'reality':
                tls_conf["reality"] = {
                    "enabled": True,
                    "public_key": params.get('pbk', [''])[0],
                    "short_id": params.get('sid', [''])[0]
                }
            outbound["tls"] = tls_conf

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

async def test_download_speed(session, proxy_url):
    """Измеряет реальную скорость СКАЧИВАНИЯ (Download Speed) в Мбит/с"""
    start_time = time.time()
    downloaded_bytes = 0
    try:
        async with session.get(TEST_DOWNLOAD_URL, proxy=proxy_url, timeout=SPEED_TEST_DURATION + 1) as response:
            if response.status != 200:
                return 0.0
            
            while True:
                chunk = await response.content.read(65536)
                if not chunk:
                    break
                downloaded_bytes += len(chunk)
                if time.time() - start_time >= SPEED_TEST_DURATION:
                    break

        elapsed = time.time() - start_time
        if elapsed <= 0 or downloaded_bytes == 0:
            return 0.0

        # Перевод в Мегабиты в секунду (Mbps)
        speed_mbps = (downloaded_bytes * 8) / (elapsed * 1_000_000)
        return round(speed_mbps, 2)
    except Exception:
        return 0.0

async def run_full_test(configs):
    outbounds = []
    config_map = {}
    tags = []
    
    for idx, cfg in enumerate(configs):
        tag = f"node-{idx}"
        sb_obj = vless_to_singbox(cfg, tag)
        if sb_obj:
            outbounds.append(sb_obj)
            config_map[tag] = cfg
            tags.append(tag)

    if not outbounds:
        return []

    # Группа-селектор для переключения активного узла при тесте скорости
    selector_group = {
        "type": "selector",
        "tag": "speed-tester",
        "outbounds": tags
    }
    outbounds.append(selector_group)

    test_config = {
        "log": {"level": "warn"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": 1080
            }
        ],
        "experimental": {
            "clash_api": {
                "external_controller": "127.0.0.1:9090"
            }
        },
        "outbounds": outbounds
    }

    with open("temp_runner.json", "w", encoding="utf-8") as f:
        json.dump(test_config, f)

    proc = subprocess.Popen(["./sing-box", "run", "-c", "temp_runner.json"])
    await asyncio.sleep(3)

    delay_passed_nodes = []
    
    # -------------------------------------------------------------
    # ЭТАП 1: Быстрый отсев по задержке (HTTP 204)
    # -------------------------------------------------------------
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
                            delay_passed_nodes.append({'config': cfg, 'delay': delay, 'tag': tag})
            except Exception:
                pass

    print(f"Ответили на запрос задержки: {len(delay_passed_nodes)} узлов.")
    if not delay_passed_nodes:
        proc.terminate()
        proc.wait()
        return []

    # Сортируем кандидатные узлы по задержке и берем ТОП-30 для детальной проверки скорости
    delay_passed_nodes.sort(key=lambda x: x['delay'])
    candidates_for_speedtest = delay_passed_nodes[:30]

    # -------------------------------------------------------------
    # ЭТАП 2: Замер скорости СКАЧИВАНИЯ (Download Speed)
    # -------------------------------------------------------------
    print("🔹 Этап 2: Тестирование СКОРОСТИ СКАЧИВАНИЯ (Download Speed)...")
    final_nodes = []
    proxy_local_url = "http://127.0.0.1:1080"

    async with aiohttp.ClientSession() as session:
        for item in candidates_for_speedtest:
            tag = item['tag']
            
            # Переключаем прокси-селектор на текущий узел через Clash API
            switch_payload = json.dumps({"name": tag}).encode('utf-8')
            req = urllib.request.Request(
                "http://127.0.0.1:9090/proxies/speed-tester",
                data=switch_payload,
                headers={"Content-Type": "application/json"},
                method="PUT"
            )
            try:
                urllib.request.urlopen(req, timeout=2)
            except Exception as e:
                print(f"Ошибка переключения селектора на {tag}: {e}")
                continue

            # Измеряем реальную скорость скачивания
            download_speed_mbps = await test_download_speed(session, proxy_local_url)
            
            if download_speed_mbps > 0.5:  # Фильтруем узлы медленнее 0.5 Мбит/с
                item['speed_mbps'] = download_speed_mbps
                final_nodes.append(item)
                print(f"⚡ Узел {tag}: Скорость скачивания = {download_speed_mbps} Мбит/с | Пинг = {item['delay']} ms")
            else:
                print(f"❌ Узел {tag}: Низкая скорость или обрыв скачивания (<0.5 Мбит/с)")

    proc.terminate()
    proc.wait()

    # Сортируем узлы в первую очередь по СКОРОСТИ СКАЧИВАНИЯ (по убыванию)
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
    print(f"Всего загружено конфигураций: {len(raw_configs)}")
    
    tested_nodes = await run_full_test(raw_configs)
    
    top_nodes = tested_nodes[:20]
    print(f"Прошли проверку скорости скачивания: {len(top_nodes)} узлов")

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
        sb_obj = vless_to_singbox(item['config'], tag)
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

    top_stats_str = "\n".join([f"• `{item['speed_mbps']} Мбит/с` (отклик {item['delay']} ms)" for item in top_nodes[:5]]) if top_nodes else "• Нет серверов, прошедших тест скорости"

    repo_name = os.environ.get('GITHUB_REPOSITORY', 'tlmanfred/vpn-auto-filter')
    msg = (
        f"🚀 **Тестирование скорости скачивания завершено!**\n\n"
        f"📊 **Результаты:**\n"
        f"• Всего обработано: `{len(raw_configs)}`\n"
        f"• Прошли тест скорости скачивания: `{len(top_nodes)}`\n\n"
        f"🏆 **ТОП-5 по скорости скачивания:**\n{top_stats_str}\n\n"
        f"🔗 **Ссылка подписки:**\n"
        f"`https://raw.githubusercontent.com/{repo_name}/main/sub.txt`"
    )
    send_telegram_message(msg)

if __name__ == "__main__":
    asyncio.run(main())
