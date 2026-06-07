# 合规文档（EBSearch）/ Compliance — EnhancedBilibiliSearch

> **本文件为简要索引。权威合规文档（canonical）在 AIVideoSummary 仓库：**
> `videoSummary/COMPLIANCE.md`
>
> EBSearch 与 AIVideoSummary **共用同一套统一账号系统**（同一 `account` 模块、同一 `users.db`、同一 `AUTH_PHONE_SALT` / `AUTH_JWT_SECRET`），因此短信验证码、PIPL、备案、支付、AIGC 标识等合规结论**完全一致**，不在此重复，请以主文档为准。

## EBSearch 专属要点

- **形态**：网站（FastAPI + 单页 Web）+ REST API；部署于中国大陆阿里云，`search.natsuki987.cn`。
- **功能**：主题 → 检索并排序 B 站视频 → 逐个总结（复用 AIVideoSummary 后端 `/api/jobs`）→ 用强模型合成**一份研究报告**。
- **账号/积分**：`ebsearch/account/` 与 AIVideoSummary 的 `account/` 代码一致；注册同样**手机号 + 邀请码 + 短信验证码**、JWT 鉴权、积分计量、**无真实支付**。计费表中含「报告基础 + 每视频 + 强模型附加」积分项。

## 与主文档对应的关键合规项（详见 canonical）

| 主题 | EBSearch 现状 | 详见主文档 |
| --- | --- | --- |
| ICP 备案 | 大陆部署，必须备案 | A/B 节（B） |
| 隐私政策 + 明示同意 | 后端门控就绪；**Web 注册 UI 同意勾选与隐私政策链接待补** | C 节 |
| AIGC 标识 | 报告**已带「生成时间 + 来源视频数」**；**「AI 生成」显式标识 + 模型名待补** | E 节 |
| 短信资质 | Mock 默认；企业签名/模板待办 | A 节 |
| 支付 | 暂缓，积分仅计量 | D 节 |
| B 站内容合规 | 报告对每条主张做 `〔BV〕`/`[n]` 出处标注并回链源视频；不再分发媒体本体 | F 节 |

## EBSearch 特有的内容合规说明

- 报告综合**多个视频**的转写/摘要，**更需严格的出处标注**：`ebsearch/render/markdown.py` 已将每条主张映射到来源视频并生成可点击链接（`_cite()`、来源表）。
- 定位为**用户主动发起的研究辅助**，输出供个人参考；避免形成对原平台内容的批量再分发与实质性替代（**平台 ToS / 著作权边界需法务确认**）。

## 隐私政策

EBSearch 用户隐私政策见本仓库 `PRIVACY.md`（内容与 AIVideoSummary 版本一致，因共用账号系统）。

---
*权威与完整内容以 `videoSummary/COMPLIANCE.md` 为准。本文件不构成法律意见。*
