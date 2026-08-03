# GitHub Actions 签到故障排查记录（2026-08-03）

## 问题背景

定时签到从某天起在 GitHub Actions 上连续失败，表现为"状态成功但实际签到失败"（run 结论 success 不等于签到成功）。

## 多次失败 run 的时间线

| run | 现象 | 日志证据 | 阶段 |
|-----|------|----------|------|
| 30782964088（早上定时） | 页面打不开 | `Cookie 有效，免密登录成功！🎉` → 签到页 `This site can't be reached` / `ERR_TIMED_OUT` | 签到页网络超时 |
| 30816417682（手动重跑） | 登录失败 | `Cookie 已失效，使用账号密码登录` → 验证码等 60s → `未触发验证码` → `登录失败` | 登录页验证码未触发 |
| 30819316536（修复后测试） | 登录失败 | 同上，但确认修复代码正常执行（重试3次+预加载CDN）→ 验证码仍不弹 | 登录页验证码未触发 |
| 30822805222（删缓存全新跑） | 登录失败 | 同上，无 cookie 可用，仍是登录页验证码未触发 | 登录页验证码未触发 |
| 30825083235（诊断） | 登录失败 | **诊断脚本证明 iframe 实际触发了**：`{"iframe":true}`；主脚本仍 `未触发验证码` → `登录失败` | 主脚本误判 |
| 30825943122（A/B） | 登录失败 | 直接访问 / 积分页重定向 **两条路径 iframe 都触发** → 导航路径无关 | 主脚本误判 |
| 30826384224（可见性） | 登录失败 | iframe `visibility:"visible", 300x150` 完全可见 → 可见性正常 | 主脚本误判 |
| 30826961179（click 对比） | 登录失败 | 原生 click / JS click 都触发 iframe；**但 `EC.visibility_of_element_located` 均超时** | 定位到 Selenium is_displayed |
| 30827418956（frame 探查） | 登录失败 | iframe `getBoundingClientRect().y = -1000000`（视口外）；`is_displayed=False`；`switch_to.frame` 成功且 frame body="安全验证" | **根因锁定** |
| 30828052461（修复1） | 登录失败 | **`触发验证码！`终于出现**，presence 等待生效；但 `获取验证码图片等元素超时`（solve 内等 slideBg 仍用 visibility） | 修复未完整 |
| 30828488004（frame 内探查） | 登录失败 | JS 拉回 iframe 生效（y -1000000→80）；但 frame 内 slideBg 仍 `displayed=False, height:0, width:270, bg:none` | slideBg 不可见 |
| 30828948382（长轮询） | 登录失败 | slideBg 高度 10×3s **恒为 0**，`bg:none`（背景图根本没加载），`ready:complete` | **headless 下不渲染** |

## 关键结论（2026-08-03 最终修正版）

- ⚠️ **"GH 连不上腾讯 CDN"是错误结论**，已被用户反证推翻：
  - 8/2 成功 run 日志明确显示 `开始下载验证码图片: https://turing.captcha.qcloud.com/cap_union_new_getcapbysig?` + OCR 识别，说明 **GH 网络能正常访问腾讯验证码 CDN**。
- ⚠️ **"登录页验证码不弹"也是错误结论**。诊断脚本铁证：
  - iframe `tcaptcha_iframe_dy` 在 GH headless 下**实际触发、内容加载完成**（`switch_to.frame` 成功，frame body = "安全验证"），`visibility=visible`、尺寸 300x150。
  - 但 `getBoundingClientRect().y = -1000000` —— **腾讯验证码把 iframe 定位在视口外 100 万像素处**（可能是预加载/动画定位），Selenium 的 `is_displayed()` 因此返回 `False`。
  - 主脚本用 `EC.visibility_of_element_located` 等 iframe → 超时 → 误报"未触发验证码"。
- 真正根因：**不是验证码没弹，而是 iframe 被腾讯 JS 定位到视口外，Selenium 可见性判断失效**。
- **更深的坑（run 30828948382 证实）**：把 iframe 拉回视口后，frame 内 `slideBg` 仍 `height:0`、`bg:none`（背景图未加载），`ready:complete`。当时误判为"headless 下不渲染"。
- **2026-08-04 本地实测推翻"headless 不渲染"结论**：真正原因是登录页 `#remember-me`（"7天免登录"）**默认勾选**，勾选时腾讯走简化验证流程 → **不下发点选图片**（slideBg 空、无目标图）。**取消勾选后 GH 与本地同样会下发 AI 点选图**（背景 img_index=1 + 目标 img_index=0）。详见 login-captcha.md。
- 导航路径无关：直接访问登录页 / 积分页重定向到登录页，两条路径 iframe 都触发。
- 点击方式无关：原生 `.click()` / JS click 都能触发。
- 修复方向正确（presence 等待 + JS 拉回视口 → "触发验证码！"出现），但 solve 内 `_download_captcha_img` 等 `slideBg` 仍用 `visibility_of_element_located`，同样会因视口外/或 frame 内元素可见性问题超时 → "获取验证码图片等元素超时"。

## 结论：GH 登录验证码处理的方向

- ~~本地 OCR（ddddocr）在 GH headless 上不可行（图片不渲染）~~ —— **已推翻**：不渲染的根因是 remember-me 勾选，非 headless。取消勾选后图片正常下发，本地 OCR / 元素截图均可取图。
- 当前方案（2026-08-04 已提交，commit e4642ba）：
  1. `checkin.py` 登录前取消勾选 `#remember-me` → 保证验证码图片下发。
  2. `TwoCaptchaProvider._download_captcha_img` 保留 URL 下载 + **新增元素截图兜底**（URL 失败/文件过小 → `slideBg` + `#instruction img` 截图），不依赖 URL 可直下性。
- 待办：手动触发 GH run 实测；确认 GH 上 URL 下载是否返回 0B（异地大概率失败），以及元素截图兜底在 GH headless 是否可用。
- 截图：诊断脚本已支持 iframe 内/全页截图（`temp/screenshots/diag_{A,B}_*.png`），artifact 现会上传该目录供下载查看（commit 0a8cb26）。

## 排查手法（可复用）

1. `gh run view <run_id> --log` 拉完整日志，重点 grep `Cookie 已失效/有效`、`未触发验证码`、`触发验证码！`、`获取验证码图片`、`登录成功/失败`。
2. 先判断 cookie 是否有效；再区分是**登录页验证码**还是**签到页验证码**失败（两者机制完全不同）。
3. **run 显示 success 不代表签到成功**，必须看日志中是否有真实"处理验证码/签到前积分/领取奖励"。
4. **诊断脚本 script/captcha_diag.py 是排查利器**（workflow 第 5.5 步会跑，`|| true` 不影响主流程）：
   - 用 `getBoundingClientRect()` 检查 iframe 真实位置，能戳穿 Selenium `is_displayed()` 的假阴性。
   - A/B 对比（直接登录 vs 积分页重定向）证明导航路径无关。
5. 重要：不要凭"GH 海外网络"想当然下结论，历史成功 run 是最好对照。

## 排查手法（可复用）

1. `gh run view <run_id> --log` 拉完整日志，重点 grep `Cookie 已失效/有效`、`未触发验证码`、`ERR_TIMED_OUT`、`登录成功/失败`、`开始下载验证码图片`。
2. 先判断 cookie 是否有效；再区分是**登录页验证码**还是**签到页验证码**失败（两者机制完全不同）。
3. **run 显示 success 不代表签到成功**，必须看日志中是否有真实"处理验证码/签到前积分/领取奖励"。
4. 本地浏览器实测登录页 DOM 与验证码触发（见 login-captcha.md）。
5. 重要：不要凭"GH 海外网络"想当然下结论，历史成功 run 是最好对照。

## 已尝试且无效的修复

- 预加载腾讯 CDN + 验证码等待超时翻倍 + 重试3次 + 登录按钮健壮 XPath —— 已 revert（提交 a7e61eb / 96494ac → revert 2694c91）。
- 删除全部 cookie 缓存再跑 —— 无效（无 cookie 时直接走登录，同样卡登录页验证码）。

## 当前修复进展（commit c4fa95b）

- `checkin.py` 登录处：`EC.visibility_of_element_located` → `EC.presence_of_element_located`（等 iframe 存在即可，不再要求可见）。
- switch 前用 JS 把 iframe 从视口外拉回屏幕中央（`position:fixed; top:80px; left:50%`）。
- 效果：`触发验证码！` 终于出现，说明能进 frame。但 solve 内 `_download_captcha_img` 等 `slideBg` 仍用 `visibility_of_element_located` → 超时报 `获取验证码图片等元素超时`。
- **下一步**：要么把 solve 内 slideBg/instruction 的 visibility 等待也改 presence + 处理坐标，要么确认 JS 拉回 iframe 后 frame 内元素是否已可见（run 30828488004 正在探查）。

## 工作流信息

- 雨云每日签到 workflow id：`286297920`（手动触发：`gh workflow run 286297920`）
- 雨云测试账号签到：`286297921`
- 每日 UTC 0:15 定时。git 提交 `0252fbc` 移除了 push 触发器，避免重复执行。
