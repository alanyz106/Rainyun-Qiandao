import logging
import os
import random
import time

from rainyun.config import import_selenium_modules, debug, linux, logger as config_logger

logger = logging.getLogger(__name__)


def get_random_user_agent(account_id: str) -> str:
    import hashlib
    import datetime
    base_date = datetime.date(2022, 3, 29)
    base_version = 100
    days_diff = (datetime.date.today() - base_date).days
    current_ver = base_version + (days_diff // 32)

    user_agents = [
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{current_ver}.0.0.0 Safari/537.36",
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{current_ver-1}.0.0.0 Safari/537.36",
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{current_ver-2}.0.0.0 Safari/537.36",
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{current_ver-10}.0) Gecko/20100101 Firefox/{current_ver-10}.0",
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{current_ver}.0.0.0 Safari/537.36 Edg/{current_ver}.0.0.0"
    ]

    account_hash = hashlib.md5(account_id.encode()).hexdigest()
    seed = int(account_hash[:8], 16)
    rng = random.Random(seed)
    return rng.choice(user_agents)


def generate_fingerprint_script(account_id: str):
    import hashlib

    account_hash = hashlib.md5(account_id.encode()).hexdigest()
    seed = int(account_hash[:8], 16)

    rng = random.Random(seed)

    webgl_vendors = [
        ("Intel Inc.", "Intel Iris Xe Graphics"),
        ("Intel Inc.", "Intel UHD Graphics 770"),
        ("Intel Inc.", "Intel UHD Graphics 730"),
        ("Intel Inc.", "Intel Iris Plus Graphics"),
        ("Intel Inc.", "Intel Arc A770"),
        ("Intel Inc.", "Intel Arc A750"),
        ("Intel Inc.", "Intel Arc B580"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 4090/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 4080 SUPER/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 4070 Ti SUPER/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 4070 SUPER/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 4070/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 4060 Ti/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 4060/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 5090/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 5080/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 5070 Ti/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 5070/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 3080/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 3070/PCIe/SSE2"),
        ("NVIDIA Corporation", "NVIDIA GeForce RTX 3060/PCIe/SSE2"),
        ("AMD", "AMD Radeon RX 7900 XTX"),
        ("AMD", "AMD Radeon RX 7900 XT"),
        ("AMD", "AMD Radeon RX 7800 XT"),
        ("AMD", "AMD Radeon RX 7700 XT"),
        ("AMD", "AMD Radeon RX 7600 XT"),
        ("AMD", "AMD Radeon RX 7600"),
        ("AMD", "AMD Radeon RX 9070 XT"),
        ("AMD", "AMD Radeon RX 9070"),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0)"),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)"),
        ("Google Inc. (Intel)", "ANGLE (Intel, Intel UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0)"),
        ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 7800 XT Direct3D11 vs_5_0 ps_5_0)")
    ]
    vendor, renderer = rng.choice(webgl_vendors)
    hardware_concurrency = rng.choice([4, 6, 8, 12, 16])
    device_memory = rng.choice([8, 16, 32])

    languages = [
        ["zh-CN", "zh", "en-US", "en"],
        ["zh-CN", "zh"],
        ["en-US", "en", "zh-CN"],
        ["zh-CN", "en-US"],
    ]
    language = rng.choice(languages)
    canvas_noise_seed = rng.randint(1, 1000000)
    audio_noise = rng.uniform(0.00001, 0.0001)
    plugins_length = rng.randint(0, 5)

    logger.debug(f"账号指纹: WebGL={renderer[:30]}..., CPU={hardware_concurrency}核, 内存={device_memory}GB")

    fingerprint_script = f"""
    (function() {{
        'use strict';

        const getParameterProxyHandler = {{
            apply: function(target, thisArg, args) {{
                const param = args[0];
                const gl = thisArg;

                if (param === 37445) {{
                    return '{vendor}';
                }}
                if (param === 37446) {{
                    return '{renderer}';
                }}
                return Reflect.apply(target, thisArg, args);
            }}
        }};

        try {{
            const originalGetParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = new Proxy(originalGetParameter, getParameterProxyHandler);
        }} catch(e) {{}}

        try {{
            const originalGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = new Proxy(originalGetParameter2, getParameterProxyHandler);
        }} catch(e) {{}}

        const noiseSeed = {canvas_noise_seed};

        function seededRandom(seed) {{
            const x = Math.sin(seed) * 10000;
            return x - Math.floor(x);
        }}

        const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type, quality) {{
            const canvas = this;
            const ctx = canvas.getContext('2d');
            if (ctx) {{
                const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
                const data = imageData.data;
                for (let i = 0; i < data.length; i += 4) {{
                    if (seededRandom(noiseSeed + i) < 0.01) {{
                        data[i] = data[i] ^ 1;
                        data[i+1] = data[i+1] ^ 1;
                    }}
                }}
                ctx.putImageData(imageData, 0, 0);
            }}
            return originalToDataURL.apply(this, arguments);
        }};

        const audioNoise = {audio_noise};

        if (window.OfflineAudioContext) {{
            const originalGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = function(channel) {{
                const result = originalGetChannelData.call(this, channel);
                for (let i = 0; i < result.length; i += 100) {{
                    const noise = Math.sin({canvas_noise_seed} + i) * audioNoise;
                    result[i] = result[i] + noise;
                }}
                return result;
            }};
        }}

        Object.defineProperty(navigator, 'hardwareConcurrency', {{
            get: () => {hardware_concurrency}
        }});

        Object.defineProperty(navigator, 'deviceMemory', {{
            get: () => {device_memory}
        }});

        Object.defineProperty(navigator, 'languages', {{
            get: () => {language}
        }});

        Object.defineProperty(navigator, 'language', {{
            get: () => '{language[0]}'
        }});

        Object.defineProperty(navigator, 'plugins', {{
            get: () => {{
                return {{
                    length: {plugins_length},
                    item: () => null,
                    namedItem: () => null,
                    refresh: () => {{}},
                    [Symbol.iterator]: function* () {{}}
                }};
            }}
        }});

        Object.defineProperty(navigator, 'webdriver', {{
            get: () => undefined
        }});

        window.chrome = {{
            runtime: {{}},
            loadTimes: function() {{}},
            csi: function() {{}},
            app: {{}}
        }};

        console.log('[Fingerprint] Browser fingerprint initialized (deterministic)');
    }})();
    """

    return fingerprint_script


def init_selenium(account_id: str, proxy: str = None):
    modules = import_selenium_modules()
    webdriver = modules['webdriver']
    Options = modules['Options']
    Service = modules['Service']

    ops = Options()
    ops.add_argument("--no-sandbox")
    ops.add_argument("--disable-dev-shm-usage")
    ops.add_argument("--disable-extensions")
    ops.add_argument("--disable-plugins")

    if proxy:
        ops.add_argument(f"--proxy-server=http://{proxy}")
        logger.info(f"浏览器已配置代理: {proxy}")

    user_agent = get_random_user_agent(account_id)
    ops.add_argument(f"--user-agent={user_agent}")
    logger.info(f"使用 User-Agent: {user_agent[:50]}...")

    if debug:
        ops.add_experimental_option("detach", True)

    ops.add_argument("--window-size=1920,1080")

    if linux:
        ops.add_argument("--headless")
        ops.add_argument("--disable-gpu")

        chromedriver_path = "/usr/bin/chromedriver"

        if os.path.exists(chromedriver_path):
            logger.info(f"使用 Docker 镜像的 ChromeDriver: {chromedriver_path}")
            service = Service(chromedriver_path)
        else:
            logger.info("使用 Selenium Manager 自动管理 ChromeDriver")
            service = Service()

        return webdriver.Chrome(service=service, options=ops)
    else:
        service = Service()
        return webdriver.Chrome(service=service, options=ops)


def get_proxy_ip():
    import requests
    import json

    proxy_api_url = os.getenv("PROXY_API_URL", "").strip()

    if not proxy_api_url:
        return None

    try:
        delay = random.uniform(0.5, 2.0)
        logger.debug(f"请求代理接口前延迟 {delay:.2f} 秒")
        time.sleep(delay)

        logger.info(f"正在从代理接口获取IP...")
        response = requests.get(proxy_api_url, timeout=10)

        if response.status_code != 200:
            logger.error(f"代理接口请求失败，状态码: {response.status_code}")
            return None

        proxy = parse_proxy_response(response.text)

        if not proxy:
            logger.error(f"代理接口返回格式无法解析: {response.text[:100]}")
            return None

        logger.info(f"获取到代理IP: {proxy}")
        return proxy

    except requests.Timeout:
        logger.error("代理接口请求超时")
        return None
    except Exception as e:
        logger.error(f"获取代理IP失败: {e}")
        return None


def parse_proxy_response(response_text):
    import json

    response_text = response_text.strip()

    try:
        data = json.loads(response_text)

        if "data" in data and isinstance(data["data"], dict):
            data = data["data"]

        if "proxy" in data:
            proxy = str(data["proxy"]).strip()
            if "://" in proxy:
                proxy = proxy.split("://")[-1]
            return proxy if ":" in proxy else None

        if "ip" in data and "port" in data:
            return f"{data['ip']}:{data['port']}"

    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    proxy = response_text.strip()

    if "://" in proxy:
        proxy = proxy.split("://")[-1]

    if ":" in proxy:
        parts = proxy.split(":")
        if len(parts) == 2:
            ip_part, port_part = parts
            if port_part.isdigit() and 1 <= int(port_part) <= 65535:
                return proxy

    return None


def validate_proxy(proxy, timeout=5, max_response_time=3):
    """
    测试代理是否可用且响应足够快。
    仅能连通不够——浏览器会话需要加载多个资源，慢代理会导致页面加载不完整、
    Cookie 无法正确送达服务器，进而被误判为"Cookie 失效"。
    :param proxy: 代理地址，格式为 ip:port
    :param timeout: 请求超时时间（秒）
    :param max_response_time: 最大允许响应时间（秒），超过则认为代理过慢
    :return: True 可用，False 不可用
    """
    import requests

    if not proxy:
        return False

    try:
        test_proxies = {
            "http": f"http://{proxy}",
            "https": f"http://{proxy}"
        }

        # 使用 app.rainyun.com 测试代理连通性（这是实际被海外 IP 拦截的目标域名）
        logger.info(f"正在验证代理 {proxy} 的可用性...")
        start_time = time.time()
        response = requests.get(
            "https://app.rainyun.com/",
            proxies=test_proxies,
            timeout=timeout
        )
        elapsed = time.time() - start_time

        if response.status_code == 200:
            if elapsed > max_response_time:
                logger.warning(f"代理 {proxy} 响应过慢（{elapsed:.1f}s > {max_response_time}s），放弃使用")
                return False
            logger.info(f"代理 {proxy} 验证成功（响应时间 {elapsed:.1f}s）")
            return True
        else:
            logger.warning(f"代理验证失败，状态码: {response.status_code}")
            return False

    except requests.exceptions.SSLError as e:
        # 代理做 HTTPS 中间人（用自己的证书替换目标站证书），requests 默认 verify=True 会拒绝。
        # 这类代理在 headless Chrome 下会触发 "Your connection is not private" → 页面打不开。
        logger.warning(f"代理 {proxy} 触发 SSL 错误，疑似 MITM 中间人代理: {str(e)[:120]}")
        return False
    except requests.Timeout:
        logger.warning(f"代理 {proxy} 验证超时")
        return False
    except Exception as e:
        logger.warning(f"代理 {proxy} 验证失败: {e}")
        return False


def check_rainyun_blocked(timeout=8):
    """
    检测当前网络环境是否被雨云拦截（海外 IP 无法访问 app.rainyun.com）。
    直连请求 app.rainyun.com，连接失败或超时则认为被拦截。
    :param timeout: 请求超时时间（秒）
    :return: True 表示被拦截（需要代理），False 表示可直连
    """
    import requests
    try:
        resp = requests.get("https://app.rainyun.com/", timeout=timeout, allow_redirects=False)
        if resp.status_code in (200, 301, 302):
            return False
        logger.warning(f"直连 app.rainyun.com 返回异常状态码 {resp.status_code}，疑似被拦截")
        return True
    except requests.Timeout:
        logger.warning("直连 app.rainyun.com 超时，疑似海外 IP 被拦截")
        return True
    except Exception as e:
        logger.warning(f"直连 app.rainyun.com 失败: {e}，疑似海外 IP 被拦截")
        return True


def get_freeproxy_ip(max_attempts=3):
    """
    使用改进版 freeproxy 抓取国内免费代理，以 app.rainyun.com 为探针并发验证，
    找到可用代理即停止。仅用于 Actions 海外环境绕过 IP 拦截。

    抓到代理后用 validate_proxy 复核（默认 verify=True），淘汰 HTTPS MITM 代理：
    freeproxy 探针可能放宽 SSL 校验，MITM 代理也能蒙混拿到 200，但 headless Chrome
    默认校验证书链会拒绝，页面显示 "Your connection is not private"。复核失败则
    重新抓取，最多 max_attempts 次。
    :param max_attempts: 抓取+复核的最大尝试次数
    :return: 代理地址字符串 "ip:port"，无可用代理时返回 None
    """
    try:
        from freeproxy.freeproxy import ProxiedSessionClient
    except ImportError:
        logger.error("未安装代理库 freeproxy，请运行 pip install -r requirements.txt")
        return None

    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

    # 代理源：仅用免浏览器抓取的国内源
    proxy_sources = [
        "KuaidailiProxiedSession", "QiyunipProxiedSession", "KxdailiProxiedSession",
        "IP89ProxiedSession", "TheSpeedXProxiedSession", "ProxyScrapeProxiedSession",
    ]

    logger.info("正在抓取国内免费代理（以 app.rainyun.com 为探针，找到即停）...")

    def _is_valid(resp):
        # 能拿到 200 响应且响应够快，才证明代理可用于浏览器会话。
        # 慢代理（>3s）会导致页面加载不完整、Cookie 无法送达，被误判为"Cookie 失效"。
        # 注意：此处无法识别 HTTPS MITM 代理——freeproxy 探针可能放宽 SSL 校验，
        # MITM 代理也能拿到 200。MITM 拦截在下方 validate_proxy 复核阶段淘汰。
        try:
            if resp.status_code != 200:
                return False
            if resp.elapsed.total_seconds() > 3:
                return False
            return True
        except Exception:
            return False

    for attempt in range(1, max_attempts + 1):
        try:
            client = ProxiedSessionClient(
                proxy_sources=proxy_sources,
                init_proxied_session_cfg={
                    "max_pages": 2,
                    "filter_rule": {"country_code": ["CN"], "protocol": ["http", "https"]},
                },
                disable_print=True,
                lazy=True,  # 不在构造阶段抓取，交给 fetch_working_streaming 边抓边验、命中即停
            )
            working = client.fetch_working_streaming(
                test_url="https://app.rainyun.com/",
                need=1,
                source_timeout=15,
                validate_timeout=5,
                validate_workers=64,
                method="GET",
                is_valid=_is_valid,
            )
        except Exception as e:
            logger.error(f"抓取国内代理失败（第 {attempt}/{max_attempts} 次）: {e}")
            continue

        if not working:
            logger.warning(f"未找到可用国内代理（第 {attempt}/{max_attempts} 次）")
            continue

        # fetch_working_streaming 返回 requests 格式字典，提取 ip:port 供 Selenium 使用
        proxy_dict = working[0]
        proxy_url = proxy_dict.get("http") or proxy_dict.get("https") or ""
        proxy_str = proxy_url.replace("http://", "").replace("https://", "").strip("/")
        if not proxy_str:
            continue
        logger.info(f"获取到候选国内代理 {proxy_str}")

        # 严格复核：用 requests 默认 verify=True 走代理访问 app.rainyun.com，
        # MITM 代理会抛 SSLError → validate_proxy 返回 False；慢代理也会被淘汰。
        # 这是 headless Chrome 真实行为的预演（Chrome 默认校验证书链）。
        if validate_proxy(proxy_str):
            logger.info(f"代理 {proxy_str} 复核通过（连通性 + SSL 证书链正常）")
            return proxy_str
        logger.warning(
            f"代理 {proxy_str} 复核未通过（第 {attempt}/{max_attempts} 次），重新抓取"
        )

    logger.warning(f"经过 {max_attempts} 次抓取，未找到通过复核的国内代理")
    return None
