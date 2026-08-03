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
        rect = f.getBoundingClientRect ? {w: f.getBoundingClientRect().width, h: f.getBoundingClientRect().height} : null;
      }
      return JSON.stringify({iframe: !!f, visibility: v, display: d, offsetSize: size, rectSize: rect, scrollW: window.innerWidth, scrollH: window.innerHeight});
    """)


def try_login(driver, tag):
    try:
        user_el = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.NAME, 'login-field')))
        pwd_el = driver.find_element(By.NAME, 'login-password')
        user_el.send_keys("gh_probe_user")
        pwd_el.send_keys("gh_probe_pass")
        btn = driver.find_element(By.XPATH, '//*[@id="app"]/div[1]/div[1]/div/div[2]/fade/div/div/span/form/button')
        print(f"[{tag}] login_btn:", btn.text.strip(), "display=", driver.execute_script("return getComputedStyle(arguments[0]).display", btn))
        driver.execute_script("arguments[0].click();", btn)
        print(f"[{tag}] clicked")
    except Exception as e:
        print(f"[{tag}] login setup ERROR", repr(e))
        return
    for i in range(10):
        time.sleep(3)
        try:
            state = check_state(driver)
            print(f"[{tag}][check {i+1}]", state)
            if '"iframe":true' in state:
                return
        except Exception as e:
            print(f"[{tag}][check {i+1}] ERROR", repr(e))


# ---- Path A: direct login URL ----
print("\n--- PATH A: direct /auth/login ---")
da = make_driver()
try:
    da.get("https://app.rainyun.com/auth/login")
    time.sleep(8)
    print("[A][url]", da.current_url)
    print("[A][title]", da.title)
    try_login(da, "A")
finally:
    da.quit()

# ---- Path B: go to earn (points) page first, redirect to login ----
print("\n--- PATH B: earn page -> redirect login ---")
db = make_driver()
try:
    db.get("https://app.rainyun.com/account/reward/earn")
    time.sleep(8)
    print("[B][url]", db.current_url)
    print("[B][title]", db.title)
    try_login(db, "B")
finally:
    db.quit()