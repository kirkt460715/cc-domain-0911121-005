# Rights Graph 数据授权目录

这是一个可独立运行的本地业务项目。核心模块保留数据接收、领域规则、持久化记录及查询接口；演练数据均为合成资料，不需要真实账号或外部业务服务。

## 运行环境

Python 3.11 或更新版本，核心业务使用标准库与 SQLite。

Dockerfile 固定运行时版本。首次准备执行 docker build -t local-product .，之后以 docker run --rm --network none local-product 运行完整检查；所有 HTTP 演练只在容器内部回环地址通信。

## 开发与运行

业务测试：
    python -m unittest discover -s tests -v
编译或语法检查：
    python -m compileall -q rights_graph demo_support
现有业务演示：
    python -m rights_graph --db :memory: demo --fixture fixtures/demo.json
输入端演练检查：
    python demo_support/check.py
获批目录采集与审查演练：
    python demo_support/harvest_drill.py
合并运行：
    sh demo_support/verify.sh

输入协议、样本与故障开关见 demo_support/协议.md。输入端检查只证明本地来源的约定行为；业务演示展示当前已有功能。调用者可以在独立临时数据目录重复运行，原始样本不会被覆盖。

## 获批目录的采集与审查

合作机构把资产、分发项与授权说明拆在相互引用的 JSON-LD 页面上。"资产已登记"不等于"授权也完整"：harvest 模块按获批入口采集页面、装配候选，再由审查人确认后才进入现有资产与授权模块。

- 来源登记（harvest.source.register）绑定获批 origin 与路径前缀，入口与重定向都不得越界；每次采集受文档数与总字节预算约束，预算用尽时运行标记 incomplete 并列出未完成的地址，资料员提高预算后再次运行即可补采。
- 采集只解释对象、数组、@graph、绝对或相对 @id 与显式 @type；不抓取远程 context，不推断未知词汇；引用环按地址去重终止；空白节点只在单页内部有效。
- 候选按资产节点装配，每个字段记录来源页面、节点与内容摘要，并关联当次采集摘要；同一字段的异文全部保留为冲突，不会以后到的页面覆盖，只能由审查人裁决或补齐，人工值单独标注来源。
- 授权说明未取齐（授权页缺失、字段不全或冲突未裁决）的候选不能确认，也不会产生可用 grant；缺失字段不补宽松默认值。
- 确认（harvest.candidate.confirm）绑定整组证据摘要与目录版本；之后任何页面变化都会把候选退回待审，需要按新证据重新确认。
- 应用（harvest.candidate.apply）通过现有 asset.create 与 grant.create 落库，已登记的资产与授权不会被采集结果覆盖或放宽；远端页面 404 只让候选证据失效，本地已有授权保持不变。
