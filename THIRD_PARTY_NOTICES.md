# 来源与第三方说明

## 设计参考

本项目参考了以下项目的功能组织与接入方式，并以独立的数据服务及原生工具接口重新实现：

- [mml-hrb/astrbot-plugin-dota2matches](https://github.com/mml-hrb/astrbot-plugin-dota2matches)：Dota2 查询、筛选、比较及数据口径方面的功能参考。参考仓库采用 MIT License，原作者马铭亮。
- [weadee/astrbot_plugin_sb6657](https://github.com/weadee/astrbot_plugin_sb6657)：工具返回资料、别名识别、缓存与按需查询方面的架构参考。参考仓库采用 MIT License。

本项目的数据服务与工具接入独立编写，没有直接复制上述项目的源码。它们不负责维护或担保本项目；本项目也不替换这些插件的文件。若后续直接引入第三方源码，需相应补充来源与许可通知。

## 运行依赖与数据源

STRATZ 适配器字段还参考了公开生成的 [STRATZ_Models schema 快照](https://github.com/TheAmazingLooser/STRATZ_Models/tree/b83999ee652a2dd5d630ce593e9928dc85110696)。这是字段契约核对来源，不代表已经针对现行接口做过运行验证；本项目未捆绑其生成代码。

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)：宿主框架，使用其工具、配置、插件视图与 Web API 接口。
- [aiohttp](https://github.com/aio-libs/aiohttp)：HTTP 客户端依赖；本项目不捆绑其源码。
- Python 标准库 `sqlite3`：本地绑定、缓存与有限查询记录。
- [OpenDota API](https://docs.opendota.com/)：公开比赛及统计数据源。数据取得能力受玩家公开状态、解析覆盖、服务限流和数据源条款约束。
- [STRATZ API](https://docs.stratz.com/)：可选的玩家资料、对局列表及单场数据来源，使用管理员自行提供的 Token。2026-10-30 已验证公开职业账号样本；详见测试报告，不附带密钥。
- [OpenDota 字段投影实现](https://github.com/odota/core/blob/8d774abda8542390fbd215487d961f1b5c3a3ef3/svc/api/spec.ts)：用于确认 `project` 参数替换默认列表字段的行为，补齐 GPM、XPM 和补刀；仅参考接口定义。

本项目的 MIT 许可仅覆盖本项目代码与文档，不代表拥有 Dota 2 商标、游戏内容或外部数据的所有权，也不改变外部数据源的使用条件。本项目与 Valve、OpenDota、STRATZ 及所参考插件作者没有官方关联。
