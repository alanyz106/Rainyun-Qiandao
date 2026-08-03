import sys, os, time, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

print("=== GH captcha diagnostic A/B ===")

def make_driver():
    ops = Options()
    ops.add_argument("--no-sandbox")
    ops.add_argument("--disable-dev-shm-usage")
    ops.add_argument("--disable-extensions")
    ops.add_argument("--disable-plugins")
    ops.add_argument("--window-size=1920,1080")
    ops.add_argument("--headless")
    ops.add_argument("--disable-gpu")

    from rainyun.browser import get_random_user_agent
    ua = get_random_user_agent("cocoty_")
    ops.add_argument(f"--user-agent={ua}")
    print("UA:", ua[:60])

    service = Service("/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=ops)
    print("chrome started")

    with open("stealth.min.js", mode="r") as f:
        js = f.read()
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": js})

    from rainyun.browser import generate_fingerprint_script
    fp = generate_fingerprint_script("cocoty_")
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": fp})
    print("fingerprint injected")
    return driver


def check_state(driver):
    return driver.execute_script("""
      var f = document.getElementById('tcaptcha_iframe_dy');
      var v = null, d = null, size = null, rect = null;
      if (f) {
        v = window.getComputedStyle(f).visibility;
        d = window.getComputedStyle(f).display;
        size = {w: f.offsetWidth, h: f.offsetHeight};
        rect = f.getBoundingClientRect ? {x: f.getBoundingClientRect().x, y: f.getBoundingClientRect().y, w: f.getBoundingClientRect().width, h: f.getBoundingClientRect().height} : null;
      }
      return JSON.stringify({iframe: !!f, visibility: v, display: d, offsetSize: size, rectSize: rect, scrollW: window.innerWidth, scrollH: window.innerHeight});
    """)


def try_login(driver, tag, click_method="native", wait_after=8):
    try:
        user_el = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.NAME, 'login-field')))
        pwd_el = driver.find_element(By.NAME, 'login-password')
        user_el.send_keys("gh_probe_user")
        pwd_el.send_keys("gh_probe_pass")
        btn = driver.find_element(By.XPATH, '//*[@id="app"]/div[1]/div[1]/div/div[2]/fade/div/div/span/form/button')
        print(f"[{tag}] login_btn:", btn.text.strip(), "display=", driver.execute_script("return getComputedStyle(arguments[0]).display", btn))
        if click_method == "js":
            driver.execute_script("arguments[0].click();", btn)
        elif click_method == "js_wait_last":
            driver.execute_script("arguments[0].click();", btn)
        else:
            btn.click()
        print(f"[{tag}] clicked (method={click_method})")
    except Exception as e:
        print(f"[{tag}] login setup ERROR", repr(e))
        return

    f = None
    for i in range(10):
        time.sleep(wait_after if i == 0 else 3)
        try:
            state = check_state(driver)
            print(f"[{tag}][check {i+1}]", state)
            if '"iframe":true' in state:
                print(f"[{tag}] === probing inside iframe ===")
                # JS: force iframe back into viewport
                moved = driver.execute_script("""
                  var f = document.getElementById('tcaptcha_iframe_dy');
                  var before = f ? f.getBoundingClientRect().y : null;
                  if (f) {
                    f.style.position = 'fixed';
                    f.style.top = '80px';
                    f.style.left = '50%';
                    f.style.margin = '0';
                    f.style.transform = 'translateX(-50%)';
                    f.style.zIndex = '2147483647';
                  }
                  var after = f ? f.getBoundingClientRect().y : null;
                  return JSON.stringify({before: before, after: after, styleTop: f?f.style.top:null});
                """)
                print(f"[{tag}] move iframe into view:", moved)
                time.sleep(1)
                driver.switch_to.frame('tcaptcha_iframe_dy')
                try:
                    # slideBg presence / visibility
                    try:
                        sb = driver.find_element(By.XPATH, '//*[@id="slideBg"]')
                        print(f"[{tag}] slideBg exists: displayed={sb.is_displayed()} size={sb.size} loc={sb.location}")
                        try:
                            st = driver.execute_script("return JSON.stringify({top: document.getElementById('slideBg').getBoundingClientRect().top, v: getComputedStyle(document.getElementById('slideBg')).visibility, d: getComputedStyle(document.getElementById('slideBg')).display, w: document.getElementById('slideBg').offsetWidth, h: document.getElementById('slideBg').offsetHeight, bg: (getComputedStyle(document.getElementById('slideBg')).backgroundImage||'').slice(0,60)})")
                            print(f"[{tag}] slideBg state:", st)
                        except Exception as e:
                            print(f"[{tag}] slideBg js state ERROR:", repr(e))
                    except Exception as e:
                        print(f"[{tag}] slideBg find ERROR: {repr(e)}")
                    # long poll: does slideBg ever grow height?
                    for pi in range(10):
                        time.sleep(3)
                        try:
                            poll = driver.execute_script("var e=document.getElementById('slideBg'); return JSON.stringify({h:e?e.offsetHeight:null, img: e?e.offsetWidth:null, ready: document.readyState})")
                            print(f"[{tag}][poll {pi+1}]", poll)
                            hh = driver.execute_script("var e=document.getElementById('slideBg'); return e?e.offsetHeight:0")
                            if hh > 0:
                                print(f"[{tag}] slideBg height grown to {hh}")
                                break
                        except Exception as e:
                            print(f"[{tag}][poll {pi+1}] ERROR:", repr(e))
                    body = driver.execute_script("return document.body?document.body.innerText.slice(0,200):'no-body'")
                    print(f"[{tag}] frame body: {body[:200]}")
                except Exception as e:
                    print(f"[{tag}] frame probe ERROR: {repr(e)}")
                driver.switch_to.default_content()
                f = True
                break
        except Exception as e:
            print(f"[{tag}][check {i+1}] ERROR", repr(e))
    if not f:
        print(f"[{tag}] RESULT: iframe NEVER appeared")


# ---- Path A: direct login, NATIVE .click() (=== main script method) ----
print("\n--- PATH A: /auth/login, native .click() ---")
da = make_driver()
try:
    da.get("https://app.rainyun.com/auth/login")
    time.sleep(8)
    print("[A][url]", da.current_url)
    print("[A][title]", da.title)
    try_login(da, "A", click_method="native")
finally:
    da.quit()

# ---- Path B: earn page -> redirect login, JS click ----
print("\n--- PATH B: earn -> redirect login, JS click() ---")
db = make_driver()
try:
    db.get("https://app.rainyun.com/account/reward/earn")
    time.sleep(8)
    print("[B][url]", db.current_url)
    print("[B][title]", db.title)
    try_login(db, "B", click_method="js")
finally:
    db.quit()