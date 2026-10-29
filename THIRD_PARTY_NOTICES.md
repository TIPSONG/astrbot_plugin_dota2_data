# 来源与第三方说明

## 设计参考

本项目参考了以下项目的功能组织与接入方式，并以独立的数据服务及原生工具接口重新实现：

- [mml-hrb/astrbot-plugin-dota2matches](https://github.com/mml-hrb/astrbot-plugin-dota2matches)：Dota2 查询、筛选、比较及数据口径方面的功能参考。参考仓库采用 MIT License，原作者马铭亮。
- [weadee/astrbot_plugin_sb6657](https://github.com/weadee/astrbot_plugin_sb6657)：工具返回资料、别名识别、缓存与按需查询方面的架构参考。参考仓库采用 MIT License。

本初始版本没有直接复制上述项目的源码。它们不负责维护或担保本项目；本项目也不替换这些插件的文件。若后续直接引入第三方源码，需相应补充来源与许可通知。

## 运行依赖与数据源

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)：宿主框架，使用其工具、配置、插件视图与 Web API 接口。
- [aiohttp](https://github.com/aio-libs/aiohttp)：HTTP 客户端依赖；本项目不捆绑其源码。
- Python 标准库 `sqlite3`：本地绑定、缓存与有限查询记录。
- [OpenDota API](https://docs.opendota.com/)：公开比赛及统计数据源。数据取得能力受玩家公开状态、解析覆盖、服务限流和数据源条款约束。

本项目的 MIT 许可仅覆盖本项目代码与文档，不代表拥有 Dota 2 商标、游戏内容或外部数据的所有权，也不改变外部数据源的使用条件。本项目与 Valve、OpenDota 及所参考插件作者没有官方关联。

