import os
import re
import json
import base64
import urllib.request
import urllib.parse
import asyncio
import aiohttp
import subprocess

SOURCES = [
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS_mobile.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS.txt",
    "https://raw.githubusercontent.com/AvenCores/goida-vpn-configs/main/githubmirror/1.txt",
    "https://raw.githubusercontent.com/AvenCores/goida-vpn-configs/main/githubmirror/26.txt"
]

TEST_DOWNLOAD_URL = "http://cachefly.cachefly.net/10mb.test"
# Снижаем нагрузку на канал GitHub Runner для более точного замера (было 8, стало 4)
CONCURRENCY_LIMIT = 4 

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
            with urllib.request.urlopen(req, timeout=15) as resp:
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

        if not host or not port or not user_info: return None

        if scheme == 'vless':
            outbound = {"type": "vless", "tag": tag_name, "server": host, "server_port": int(port), "uuid": user_info}
            flow = params.get('flow', [None])[0]
            if flow: outbound["flow"] = flow
            security = params.get('security', ['none'])[0]
            if security in ['tls', 'reality']:
                sni = params.get('sni', [host])[0]
                tls_conf = {"enabled": True, "server_name": sni, "insecure": True}
                if security == 'reality':
                    pbk = params.get('pbk', [''])[0]
                    if not pbk: return None
                    tls_conf["reality"] = {"enabled": True, "public_key": pbk, "short_id": params.get('sid', [''])[0]}
                outbound["tls"] = tls_conf
        elif scheme == 'trojan':
            outbound = {"type": "trojan", "tag": tag_name, "server": host, "server_port": int(port), "password": user_info, "tls": {"enabled": True, "server_name": params.get('sni', [host])[0], "insecure": True}}
        else:
            return None

        transport = params.get('type', ['tcp'])[0]
        if transport == 'ws':
            outbound["transport"] = {"type": "ws", "path": params.get('path', ['/'])[0], "headers": {"Host": params.get('host', [host])[0]}}
        elif transport == 'grpc':
            outbound["transport"] = {"type": "grpc", "service_name": params.get('serviceName', [''])[0]}
        return outbound
    except Exception:
        return None

async def check_single_delay(session, tag):
    """Асинхронный пинг. Увеличен таймаут ожидания до 5 секунд (5000мс)."""
    test_url = f"http://127.0.0.1:9090/proxies/{urllib.parse.quote(tag)}/delay?url=http://cp.cloudflare.com/generate_204&timeout=5000"
    try:
        # Сессии даем чуть больше времени, чем самому Clash
        async with session.get(test_url, timeout=7.0) as resp:
            if resp.status == 200:
                data = await resp.json()
                return tag, data.get("delay", 0)
    except:
        pass
    return tag, 0

async def measure_speed_concurrently(item, worker_id, sem):
    """Глубокий замер скорости."""
    async with sem:
        port = 10000 + worker_id 
        conf_path = f"temp_single_{worker_id}.json"
        
        single_config = {
            "log": {"level": "error"},
            "inbounds": [{"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": port}],
            "outbounds": [item['sb_obj']]
        }
        with open(conf_path, "w", encoding="utf-8") as f:
            json.dump(single_config, f)

        sb_proc = await asyncio.create_subprocess_exec("./sing-box", "run", "-c", conf_path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        
        # ДАЕМ ВРЕМЯ НА HANDSHAKE: 4 секунды (хватит даже самым медленным Reality-серверам)
        await asyncio.sleep(4.0) 

        speed_mbps = 0.0
        # CURL: 10 сек на подключение, 20 сек на скачивание. Позволяет измерить реальную среднюю скорость.
        curl_cmd = [
            "curl", "-k", "-s", "-o", "/dev/null", "-w", "%{speed_download}",
            "--connect-timeout", "10", "--max-time", "20",
            "-x", f"http://127.0.0.1:{port}", TEST_DOWNLOAD_URL
        ]
        
        try:
            curl_proc = await asyncio.create_subprocess_exec(*curl_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            # Ждем завершения curl с запасом (25 сек)
            stdout, _ = await asyncio.wait_for(curl_proc.communicate(), timeout=25.0)
            out = stdout.decode().strip()
            speed_bytes_sec = float(out) if out else 0.0
            speed_mbps = round((speed_bytes_sec * 8) / 1_000_000, 2)
        except Exception:
            pass
        finally:
            try: sb_proc.terminate()
            except: pass
            
        item['speed_mbps'] = speed_mbps
        if speed_mbps > 0.05:
            print(f"✅ {item['tag']}: {speed_mbps} Мбит/с | Пинг: {item['delay']} ms")
        else:
            print(f"❌ {item['tag']}: 0 Мбит/с | Пинг: {item['delay']} ms (Таймаут или обрыв)")
            
        return item

async def run_full_test(configs):
    node_outbounds = []
    config_map = {}
    
    for idx, cfg in enumerate(configs):
        tag = f"node-{idx}"
        sb_obj = config_to_singbox(cfg, tag)
        if sb_obj:
            node_outbounds.append(sb_obj)
            config_map[tag] = cfg

    print(f"Сконвертировано узлов: {len(node_outbounds)}")
    if not node_outbounds: return []

    test_config = {
        "log": {"level": "error"},
        "experimental": {"clash_api": {"external_controller": "127.0.0.1:9090"}},
        "outbounds": node_outbounds
    }
    with open("temp_runner.json", "w", encoding="utf-8") as f:
        json.dump(test_config, f)

    proc = subprocess.Popen(["./sing-box", "run", "-c", "temp_runner.json"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    await asyncio.sleep(4) # Даем больше времени ядру на парсинг сотен конфигов

    print("🔹 Этап 1: МАССОВЫЙ асинхронный пинг (таймаут 5 сек)...")
    delay_passed_nodes = []
    
    async with aiohttp.ClientSession() as session:
        tasks = [check_single_delay(session, tag) for tag in config_map.keys()]
        results = await asyncio.gather(*tasks) 
        
        for tag, delay in results:
            if delay > 0:
                delay_passed_nodes.append({'config': config_map[tag], 'delay': delay, 'tag': tag, 'sb_obj': config_to_singbox(config_map[tag], 'main-out')})

    proc.terminate()
    proc.wait()

    print(f"Ответило на пинг (HTTP 204): {len(delay_passed_nodes)} узлов.")
    if not delay_passed_nodes: return []

    delay_passed_nodes.sort(key=lambda x: x['delay'])
    
    # Расширяем воронку проверки: тестируем скорость для ТОП-60 серверов
    candidates = delay_passed_nodes[:60] 

    print(f"🔹 Этап 2: Точный замер скорости ({CONCURRENCY_LIMIT} потока, до 20 сек на узел)...")
    sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
    
    speed_tasks = []
    for i, item in enumerate(candidates):
        worker_id = i % CONCURRENCY_LIMIT 
        speed_tasks.append(measure_speed_concurrently(item, worker_id, sem))
        
    final_nodes = await asyncio.gather(*speed_tasks)

    valid_nodes = [n for n in final_nodes if n['speed_mbps'] > 0.05]
    
    if not valid_nodes:
        print("⚠️ Ни один узел не пробил тест скорости. Отдаем по минимальному пингу!")
        for item in candidates[:20]:
            item['speed_mbps'] = 0.0
            valid_nodes.append(item)

    valid_nodes.sort(key=lambda x: x['speed_mbps'], reverse=True)
    return valid_nodes

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
    tested_nodes = await run_full_test(raw_configs)
    
    # Сохраняем ТОП-25 самых быстрых и стабильных
    top_nodes = tested_nodes[:25] 
    out_configs = [item['config'] for item in top_nodes]
    
    with open("sub.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(out_configs))

    sb_outbounds = []
    tags = []
    for idx, item in enumerate(top_nodes):
        tag = f"node-{idx+1}-{item['speed_mbps']}Mbps"
        sb_obj = config_to_singbox(item['config'], tag)
        if sb_obj:
            sb_outbounds.append(sb_obj)
            tags.append(tag)

    if tags:
        sb_outbounds.append({"type": "urltest", "tag": "auto-outbound", "outbounds": tags, "url": "http://cp.cloudflare.com/generate_204", "interval": "5m"})
    with open("nodes.json", "w", encoding="utf-8") as f:
        json.dump({"outbounds": sb_outbounds}, f, indent=2, ensure_ascii=False)

    top_stats_str = "\n".join([f"• `{item['speed_mbps']} Мбит/с` ({item['delay']} ms)" for item in top_nodes[:5]]) if top_nodes else "• Нет серверов"
    repo_name = os.environ.get('GITHUB_REPOSITORY', 'tlmanfred/vpn-auto-filter')
    msg = (f"🚀 **Глубокое тестирование завершено!**\n\n"
           f"📊 **Результаты:**\n• Обработано: `{len(raw_configs)}`\n• Отобрано в подписку: `{len(top_nodes)}`\n\n"
           f"🏆 **ТОП-5 по скорости:**\n{top_stats_str}\n\n"
           f"🔗 **Подписка:**\n`https://raw.githubusercontent.com/{repo_name}/main/sub.txt`")
    send_telegram_message(msg)

if __name__ == "__main__":
    asyncio.run(main())
