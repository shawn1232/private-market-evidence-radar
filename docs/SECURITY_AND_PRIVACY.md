# Security & Privacy｜安全与隐私

DealScope 当前定位是本地单用户研究工具，而不是面向公网的多租户服务。

## 已实现边界

- 两个 Flask 服务默认只绑定 `127.0.0.1`。
- 产生副作用的本地路由检查请求来源；外部 Origin 会被拒绝。
- HTTP 抓取只接受允许的协议，并在 DNS 解析和每次重定向后重新检查目标，阻止 loopback、私网和链路本地地址。
- 非文本与超大响应会在进入解析层之前被拒绝。
- 公众号文章库只写入白名单字段；cookie、authorization、token、password、session 等凭据键不会进入文章表。
- JSON 缓存使用原子替换；SQLite 导入使用事务与 preview-confirm 流程。
- 页面中的外部文本采用转义或 `textContent` 渲染，不作为 HTML 执行。

## 公开仓库规则

不要提交：

- `sessions/` 或浏览器 storage state；
- `data/raw/` 中的原文、截图或受限制材料；
- `data/wechat_pool/` 中的真实文章库；
- `data/output/` 中的真实候选、评分或刷新错误；
- API key、cookie、token、二维码、日志和本地绝对路径。

演示应使用 `examples/` 与 `scripts/load_demo.py`。所有合成 URL 使用 `.invalid` 保留域名，避免误连真实站点。

## 外部服务

公开搜索、LLM 和需要登录的来源均为可选能力。启用前应阅读对应服务条款、数据处理政策和内容使用限制。不要将未公开项目信息发送给未经批准的第三方服务。

公众号原文读取会优先直连原始公开 URL。可选的第三方公开正文回退会把该公开文章 URL 发送到配置的外部服务，因此默认关闭；仅在明确接受这一数据流后设置 `DEALSCOPE_ALLOW_PUBLIC_WECHAT_FALLBACK=1`。

## 非目标

当前版本不承诺：

- 公网部署安全；
- 多用户身份与权限隔离；
- 企业级密钥管理；
- 全量审计日志与数据留存策略；
- 对第三方网页内容的再分发权利。

若部署范围超出本机，需要在现有边界之外增加鉴权、CSRF token、反向代理、任务队列、数据库迁移、密钥管理和正式安全审计。
