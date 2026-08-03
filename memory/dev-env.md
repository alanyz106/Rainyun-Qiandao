# 本地开发与调试环境（Windows）

## 环境要点

- OS：Windows，Shell：PowerShell 5.1。Python 命令用 `python`（不是 python3）。
- 本地无 `.env`（正式账号密码只存在 GitHub Secrets 里），无法本地直接重放正式登录。

## 调试 GH 问题的方法

- 用 `gh run view <run_id> --log`（可接 `Select-String` 过滤）看 CI 日志。
- 用 kimi-webbridge 控制真实浏览器核对登录页 DOM / 验证码弹窗（见下）。

## kimi-webbridge（控制真实浏览器调试）

- daemon：`~/.kimi-webbridge/bin/kimi-webbridge`，API `http://127.0.0.1:10086/command`。
- 常用动作：`navigate`、`evaluate`、`list_tabs`、`close_session`；session 名 `rainyun`。
- 首次连接如报 "session tab was closed"，先 navigate newTab 重建。

## PowerShell 坑（血泪）

- **`curl` 是 Invoke-WebRequest 别名**，调 REST API 必须用 `curl.exe`。
- 向 API 发 JSON：**反引号/引号转义在 PowerShell 里极易出错**，正确做法是把 JSON 写入临时文件再 `-d "@file"`：
  ```powershell
  $body = '{"action":"navigate","args":{"url":"https://..."},"session":"rainyun"}'
  Set-Content -LiteralPath "$env:TEMP\kwb_req.json" -Value $body -NoNewline
  curl.exe -s -X POST http://127.0.0.1:10086/command -H "Content-Type: application/json" -d "@$env:TEMP\kwb_req.json"
  ```
- 链式命令用 `;` 或 `if ($?) {...}`，**没有 `&&`**。

## 本地浏览器调试验证过的关键结论

- 登录页输入框 `name=login-field` / `login-password` 有效（2026-08-03）。
- preconnect 注入脚本执行正常，验证码 iframe 在点击登录后能正常弹出。
- ⚠️ 本地能弹验证码只证明脚本没写错，**不能证明 GH 数据中心网络能连上腾讯 CDN** —— 必须以 GH 实际 run 为准。

## 本地 Selenium 调试（2026-08-04 新增）

- 本地 Chrome 在 `C:\Program Files\Google\Chrome\Application\chrome.exe`（版本 150.0.7871.187）。
- ChromeDriver 在 `C:\Users\alan\AppData\Local\Temp\opencode\chromedriver\chromedriver-win64\chromedriver.exe`（150.0.7871.124，与 Chrome 匹配）。
- ⚠️ Selenium Manager 自动下载 driver 会因访问 googlechromelabs.github.io 失败而报错（`Unable to obtain driver`），**必须显式指定 driver 路径**：
  `webdriver.Chrome(service=Service(r"C:\Users\alan\AppData\Local\Temp\opencode\chromedriver\chromedriver-win64\chromedriver.exe"), options=ops)`
- 调试验证码用 `--headless` 之外真实窗口 + `ops.add_experimental_option("detach", True)` 可保留窗口人工观察。
- 触发登录验证码的假账号：用户名/密码需**足够长**（如 `probe_user_local_2026` / `Probe_pass_2026_Strong_#88`），过短会被前端拦截。
- **务必先取消勾选 `#remember-me`**，否则验证码图片不下发（详见 login-captcha.md）。
