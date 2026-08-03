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

print("=== GH captcha diagnostic ===")

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

try:
    driver.get("https://app.rainyun.com/auth/login")
    time.sleep(8)
    print("[url]", driver.current_url)
    print("[title]", driver.title)

    user_el = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.NAME, 'login-field')))
    pwd_el = driver.find_element(By.NAME, 'login-password')
    user_el.send_keys("gh_probe_user")
    pwd_el.send_keys("gh_probe_pass")

    btn = driver.find_element(By.XPATH, '//*[@id="app"]/div[1]/div[1]/div/div[2]/fade/div/div/span/form/button')
    print("[login_btn]", btn.text.strip(), "display=", driver.execute_script("return getComputedStyle(arguments[0]).display", btn))
    btn.click()
    print("[clicked]")

    for i in range(10):
        time.sleep(3)
        state = driver.execute_script("""
          var f = document.getElementById('tcaptcha_iframe_dy');
          var turing = document.querySelectorAll('script[src*="turing"], script[src*="captcha"]').length;
          var turingRes = performance.getEntriesByType('resource').filter(e=>e.name.includes('turing')||e.name.includes('TCaptcha')||e.name.includes('captcha.gtimg')).map(e=>e.name);
          return JSON.stringify({iframe: !!f, iframeSrc: f?f.src.substring(0,80):null, turingScripts: turing, resources: turingRes});
        """)
        print(f"[check {i+1}]", state)
        if '"iframe":true' in state:
            break

    body = driver.execute_script("return document.body ? document.body.innerText.slice(0,200) : ''")
    print("[body]", body.replace(chr(10), ' | '))
except Exception as e:
    import traceback
    print("[ERROR]", traceback.format_exc())
finally:
    try:
        os.makedirs("temp", exist_ok=True)
        driver.save_screenshot("temp/captcha_diag.png")
        print("[screenshot] saved")
    except Exception as e:
        print("[screenshot fail]", e)
    driver.quit()
