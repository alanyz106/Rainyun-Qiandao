# 登录验证码机制与代码修复

## 验证码触发机制（2026-08-03 实测确认）

- ⚠️ **登录页 `#remember-me`（"7天免登录"）默认勾选**（checked=true）。勾选时腾讯走**简化验证流程 → 不下发点选图片**（slideBg 无 background、无目标图，看起来像"错误提醒"）。**取消勾选后才正常下发 AI 点选图**（背景大图 img_index=1 + 目标图案 img_index=0）。
- 雨云登录页**点击"登录"按钮即触发腾讯验证码**（TCaptcha），与账号密码是否正确无关。假账号点登录照样弹验证码。
- 验证码 iframe id：`tcaptcha_iframe_dy`，src 指向 `https://turing.captcha.gtimg.com/1/template/drag_ele.html`。
- 依赖 JS：`https://turing.captcha.qcloud.com/TCaptcha.js`（TencentCaptcha 初始化）。
- 流程：点登录 → 前端调 TCaptcha → 验证码 iframe 插入 → 通过后表单才提交后端校验账号密码。

## 登录页 DOM（本地浏览器实测，2026-08-03 仍有效）

- 账号输入框：`input[name="login-field"]`（id=`field`）
- 密码输入框：`input[name="login-password"]`（id=`__BVID__69`）
- 登录按钮：`form.auth-login-form` 内 `button[type="submit"]`，文本"登 录"
- 代码中使用的健壮 XPath：
  `//form[contains(@class, "auth-login-form")]//button[@type="submit"]`
  （曾用绝对路径 `//*[@id="app"]/div[1]/div[1]/div/div[2]/fade/...`，易随前端结构变化失效，已弃）

## checkin.py 本次修复（commit a7e61eb 基础上）

1. `preload_captcha_cdn(driver)` — 点击登录前注入 `preconnect`（turing.captcha.qcloud.com / turing.captcha.gtimg.com）+ no-cors 预取 `TCaptcha.js`，缓解海外网络首次加载慢。
2. `wait_login_captcha(...)` — 等 iframe `tcaptcha_iframe_dy`，超时从 30s 提升到 `max(timeout*2, 60)`；最多重试 3 次，超时自动重新点击登录按钮并重新预加载 CDN。
3. 登录按钮 XPath 改为基于 `form.auth-login-form` 的相对定位。

## 坑：Selenium 懒加载模式（重要）

本项目 selenium 模块采用**懒加载**（见 config.py 的 `import_selenium_modules()`），**模块顶层没有 WebDriverWait 等名称**。
- 在自定义函数里用 `WebDriverWait` 必须先 `modules = import_selenium_modules()` 再取。
- 曾因漏掉这步导致 GH 上报 `name 'WebDriverWait' is not defined`（commit e6a183a 修复）。
- 参考现有写法：`dismiss_modal_confirm`、`wait_captcha_or_modal` 都是函数内先 `import_selenium_modules()`。

## 坑：iframe 被定位到视口外，Selenium is_displayed 假阴性（GH 根因，2026-08-03）

- 登录页验证码 iframe 在 GH headless 下**实际弹出且内容加载完成**（`switch_to.frame` 成功，body="安全验证"），`visibility:visible`、300x150。
- 但 `getBoundingClientRect().y = -1000000` —— **腾讯验证码把 iframe 定位在视口外 100 万像素**。
- Selenium `is_displayed()` 对视口外元素返回 `False` → `EC.visibility_of_element_located((By.ID,'tcaptcha_iframe_dy'))` 超时 → 主脚本误报"未触发验证码"。
- **教训**：判断 iframe 是否弹出，用 `presence_of_element_located` 或 JS 查 `getBoundingClientRect()`，不要用 `visibility_of_element_located`。
- 修复：登录处改 presence + switch 前 JS 拉回视口（`position:fixed; top:80px; left:50%; transform:translateX(-50%)`）。
- 同类坑：验证码 solve 内 `_download_captcha_img` 等 `slideBg`/`instruction` 也用了 visibility 等待，同样会超时（"获取验证码图片等元素超时"），后续可能也需要改 presence 或先拉回视口。

## 验证码图片获取方式（2026-08-03 本地实测，两条必知）

- 验证码背景/目标图**不是 data:base64**，也不是纯 CSS background URL 可直下——是腾讯接口 `cap_union_new_getcapbysig?img_index=0|1&image=...&sess=...`，
  背景大图在 `#slideBg` 的 `background-image`（约 340x243），目标图案在 `#instruction/div/img`（naturalWidth ~170，含 3 个目标图）。
- ⚠️ 该 URL **requests/浏览器外直接 GET 拿不到内容**（返回 0B text/plain，需浏览器内签名）。所以**不要依赖 URL 解析**。
- **正解：元素截图** `slideBg.screenshot()` 与 `#instruction/div/img`.screenshot()，跳过 URL 下载直接拿像素图（本地生成 captcha.jpg 41KB / sprite.jpg 2KB）。
- 本地（本机 IP）requests 偶然能下载成功，但 GH/异地不可靠，元素截图才是通用兜底。

## TwoCaptchaProvider 修复（只增不删）

- `_download_captcha_img` 保留原 URL 下载逻辑；**新增兜底**：URL 下载失败/文件过小(<1000B)时，改用 `slideBg` + `#instruction img` 元素截图生成 `temp/captcha.jpg`/`sprite.jpg`。
- `checkin.py` 登录处登录前**取消勾选 `#remember-me`**，确保验证码图片下发（GH 默认勾选，不勾则无图）。

## 验证码识别

- 识别率约 48.3%，多次重试最终能通过；支持 2captcha 备用（TWOCAPTCHA_API_KEY）。
- 相关历史修复：`cd2f49f` 2captcha 坐标解析、`877d651` 2captcha 识别重试。
