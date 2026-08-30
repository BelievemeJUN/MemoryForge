"""检索级评测：建测试知识库（24 篇主题文档）并生成检索题库。

流程：
1. 定义 24 篇主题文档（每篇含独特术语，query 用术语提问可精确召回）
2. 建知识库 eval_retrieval_kb（幂等：先删旧）
3. 灌入 Milvus 子块（bge-large-zh 1024 维向量 + BM25 稀疏）+ PG 父块
4. 生成 retrieval_cases.yaml（query + expected_keyword，供 run_eval 检索分支用）

用法：../.venv/bin/python evals/seed_retrieval_kb.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml  # noqa: E402
from langchain_core.documents import Document  # noqa: E402

KB_ID = "eval_retrieval_kb"
USER_ID = 1

# (id, 标题, 正文[含独特术语], 检索 query, 期望命中的独特关键词)
DOCS = [
    ("doc_01", "Python 列表推导式", "Python 的列表推导式（list comprehension）用一行表达式快速生成列表，例如 [x*x for x in range(10)] 生成 0 到 9 的平方。它比 for 循环更简洁高效。", "Python 怎么用列表推导式快速生成平方列表", "列表推导式"),
    ("doc_02", "Python 装饰器", "Python 装饰器（decorator）用 @ 语法给函数附加行为，常用于日志、鉴权、性能计时。装饰器本质是接收函数返回新函数的高阶函数。", "Python 装饰器怎么实现日志功能", "装饰器"),
    ("doc_03", "冒泡排序", "冒泡排序（bubble sort）通过相邻元素两两比较交换，把最大元素逐步冒泡到末尾。时间复杂度 O(n^2)，适合小规模数据。", "冒泡排序的时间复杂度是多少", "O(n^2)"),
    ("doc_04", "快速排序", "快速排序（quick sort）选取基准值，把数组分成小于和大于两部分递归排序，平均时间复杂度 O(n log n)，是工程中最常用的排序之一。", "快速排序平均时间复杂度", "O(n log n)"),
    ("doc_05", "二分查找", "二分查找（binary search）要求数组有序，每次取中间值比较，把搜索范围减半，时间复杂度 O(log n)。", "有序数组里二分查找的时间复杂度", "O(log n)"),
    ("doc_06", "哈希表", "哈希表（hash table）通过哈希函数把键映射到桶，实现平均 O(1) 的插入与查找。Python 的 dict 就是哈希表实现。", "Python 的 dict 底层是什么数据结构", "哈希表"),
    ("doc_07", "二叉树遍历", "二叉树遍历分前序、中序、后序，前序为根左右，中序为左根右，后序为左右根；层序遍历用队列实现。", "二叉树中序遍历的顺序", "左根右"),
    ("doc_08", "动态规划", "动态规划（dynamic programming）把大问题拆成重叠子问题，用状态转移方程自底向上求解，经典例子是 0-1 背包问题。", "0-1 背包问题用什么算法", "动态规划"),
    ("doc_09", "Redis 缓存", "Redis 是内存键值数据库，支持字符串、哈希、列表、有序集合等数据结构，常用于缓存、限流、分布式锁，单线程模型保证原子性。", "什么数据库适合做缓存和分布式锁", "Redis"),
    ("doc_10", "PostgreSQL 事务", "PostgreSQL 支持 ACID 事务，通过 MVCC 多版本并发控制实现读写不阻塞，事务隔离级别可配置为读已提交、可重复读等。", "PostgreSQL 靠什么机制实现并发控制", "MVCC"),
    ("doc_11", "Milvus 向量检索", "Milvus 是开源向量数据库，支持稠密向量与稀疏向量混合检索，用 RRF 融合排序，适合 RAG 场景的语义召回。", "RAG 场景用什么数据库做向量召回", "Milvus"),
    ("doc_12", "Docker 容器", "Docker 用容器隔离进程，镜像分层复用，通过 namespace 和 cgroup 实现资源隔离与限制，一条 docker run 即可启动应用。", "用什么技术把应用打包成可隔离运行的镜像", "Docker"),
    ("doc_13", "Kubernetes 编排", "Kubernetes（K8s）是容器编排平台，管理 Pod 的调度、副本、滚动更新与服务发现，支持声明式配置。", "容器太多怎么自动调度和管理", "Kubernetes"),
    ("doc_14", "Linux 文件权限", "Linux 文件权限用 rwx 三组表示属主、属组、其他人，chmod 755 表示属主可读写执行，其他人可读执行。", "Linux 中 chmod 755 表示什么", "755"),
    ("doc_15", "HTTP 状态码", "HTTP 状态码 200 表示成功，404 表示资源不存在，500 表示服务器内部错误，429 表示请求过多被限流。", "HTTP 429 状态码表示什么", "429"),
    ("doc_16", "RESTful API", "RESTful API 用 HTTP 方法表达操作：GET 查询、POST 新建、PUT 更新、DELETE 删除，资源用 URL 标识，是无状态设计。", "RESTful 接口怎么用 HTTP 方法表达增删改查", "RESTful"),
    ("doc_17", "WebSocket", "WebSocket 建立一次 TCP 连接后双向通信，适合实时推送场景如聊天、任务进度，通过 upgrade 握手从 HTTP 升级。", "实时推送聊天消息用什么协议", "WebSocket"),
    ("doc_18", "JWT 认证", "JWT（JSON Web Token）是无状态令牌，分 header、payload、signature 三段，用 HMAC 签名防篡改，含过期时间 exp 与唯一 id jti。", "无状态令牌认证用什么标准", "JWT"),
    ("doc_19", "SQL 索引", "SQL 索引用 B+ 树加速查询，主键自动建索引，覆盖索引可避免回表，索引过多会拖慢写入性能。", "数据库用什么结构加速查询", "B+ 树"),
    ("doc_20", "Git 分支策略", "Git 分支策略常用 git flow：main 主分支、develop 开发分支、feature 功能分支，合并用 pull request 走代码评审。", "团队协作怎么用 Git 分支管理", "git flow"),
    ("doc_21", "公司报销制度", "公司报销制度：差旅报销需在出差结束后 7 个工作日内提交申请，附发票与行程单，超过 5000 元需部门总监审批。", "差旅报销要在几天内提交", "7 个工作日"),
    ("doc_22", "员工请假政策", "员工请假政策：年假需提前 3 天申请，病假需当日提交病假条，事假每次不超过 3 天，全年累计不超过 15 天。", "年假要提前几天申请", "3 天"),
    ("doc_23", "产品发布流程", "产品发布流程：开发完成 → 代码评审 → 灰度发布（10% 用户）→ 观察 24 小时 → 全量上线；异常可一键回滚到上一版本。", "新版本上线前要先经历什么阶段", "灰度"),
    ("doc_24", "客服响应规范", "客服响应规范：普通咨询 5 分钟内响应，紧急工单 30 分钟内升级给技术值班，重大故障需 10 分钟内拉起应急群并同步管理层。", "重大故障客服要在几分钟内拉应急群", "10 分钟"),
]

# 干扰文档（语义相近，制造检索难度——只灌库不单独出题）
# 分组：Python 语法 / 排序 / 查找与数据结构 / 公司制度 / 产品客服
DISTRACT_DOCS = [
    ("doc_dpy1", "Python while 循环", "Python 的 while 循环在条件为真时反复执行代码块，例如 while count < 5: 会持续执行直到 count 达到 5，适合不知道循环次数的场景。"),
    ("doc_dpy2", "Python for 循环", "Python 的 for 循环遍历可迭代对象（列表、字符串、range），for x in items: 每次取一个元素，适合遍历已知集合。"),
    ("doc_dpy3", "Python 异常处理", "Python 用 try/except 捕获异常，try 里放可能出错的代码，except 捕获后处理，避免程序崩溃，finally 保证收尾。"),
    ("doc_dpy4", "Python lambda", "Python 的 lambda 定义单行匿名函数，lambda x: x*2 等价于一个小函数，常用于 map/filter 或排序的 key 回调。"),
    ("doc_ds1", "插入排序", "插入排序把每个元素逐个插入到已排序部分，平均时间复杂度 O(n^2)，数据量小时效率不错。"),
    ("doc_ds2", "选择排序", "选择排序每轮选出最小值放到前面，时间复杂度 O(n^2)，实现简单但交换次数较多。"),
    ("doc_ds3", "归并排序", "归并排序用分治法，把数组分成两半递归排序再合并，稳定排序，时间复杂度 O(n log n)，适合大数据量。"),
    ("doc_ds4", "堆排序", "堆排序利用堆结构原地排序，时间复杂度 O(n log n)，不稳定但空间复杂度 O(1)。"),
    ("doc_dd1", "线性查找", "线性查找从头到尾逐个比较，时间复杂度 O(n)，适合无序的小数组。"),
    ("doc_dd2", "栈", "栈是后进先出（LIFO）结构，push 入栈、pop 出栈，适合括号匹配、函数调用栈等场景。"),
    ("doc_dd3", "队列", "队列是先进先出（FIFO）结构，入队 enqueue、出队 dequeue，适合任务调度、消息队列等按到达顺序处理的场景。"),
    ("doc_dd4", "链表", "链表由节点串联，已知位置时插入删除 O(1)，查询 O(n)，分单链表与双链表。"),
    ("doc_dc1", "考勤制度", "公司考勤：工作日 9:00-18:00，弹性办公可 10:00 前到岗，迟到超过 30 分钟记一次警告。"),
    ("doc_dc2", "信息安全制度", "信息安全：办公电脑必须加密硬盘，访问生产库需双人审批，禁止私带外部存储设备。"),
    ("doc_dc3", "培训制度", "培训制度：新员工入职两周内完成安全培训与工具培训，年度须完成 20 学时线上课程。"),
    ("doc_dc4", "采购制度", "采购流程：低于 1 万元由部门负责人审批，1-10 万元需财务总监审批，超过 10 万元走招投标。"),
    ("doc_dp1", "版本命名规范", "版本命名：主版本.次版本.补丁号，重大不兼容升主版本，新增功能升次版本，修复 bug 升补丁号。"),
    ("doc_dp2", "工单分级", "工单分级：P0 紧急（影响核心功能）、P1 高（大范围受影响）、P2 中、P3 低，P0 需立即响应。"),
    ("doc_dp3", "SLA 承诺", "SLA 承诺：系统可用性 99.9%，月度故障响应 15 分钟内，修复目标 P0 4 小时、P1 24 小时。"),
    ("doc_dp4", "反馈渠道", "用户反馈渠道：App 内反馈、客服热线、邮件 support@company.com，重大建议进入产品评审。"),
    # 通用领域干扰（进一步稀释检索空间，制造更真实的召回难度）
    ("doc_g01", "MySQL 索引", "MySQL 的 InnoDB 存储引擎使用 B+ 树索引，主键索引叶子节点存整行数据，辅助索引叶子节点存主键。"),
    ("doc_g02", "TCP 三次握手", "TCP 建立连接通过三次握手：SYN、SYN-ACK、ACK，确认双方收发能力后才开始传数据。"),
    ("doc_g03", "前端事件冒泡", "DOM 事件冒泡从目标元素逐级向上传播到 document，可以在父节点统一处理子元素事件。"),
    ("doc_g04", "反向代理", "Nginx 常作反向代理，把客户端请求转发给后端多台服务器，兼做负载均衡与静态资源缓存。"),
    ("doc_g05", "机器学习过拟合", "模型在训练集表现好但测试集差称为过拟合，常用正则化、交叉验证、数据增强缓解。"),
    ("doc_g06", "AES 加密", "AES 是对称加密算法，用同一个密钥加解密，常用 128/256 位密钥，广泛用于数据传输加密。"),
    ("doc_g07", "云服务器伸缩", "云服务支持弹性伸缩，根据 CPU 使用率自动增加或减少实例数量，应对流量波动。"),
    ("doc_g08", "微服务拆分", "微服务按业务边界拆分成独立服务，各自独立部署，通过 API 网关统一入口，服务间用消息或 HTTP 通信。"),
    ("doc_g09", "单元测试", "单元测试针对函数或方法做隔离验证，用 mock 隔离外部依赖，保证单点逻辑正确。"),
    ("doc_g10", "CI 持续集成", "持续集成在每次代码提交后自动拉取、编译、跑测试，快速反馈集成问题，配合流水线自动部署。"),
    ("doc_g11", "操作系统进程调度", "操作系统按优先级与时间片调度进程，抢占式调度保证交互响应，进程间通过 IPC 通信。"),
    ("doc_g12", "编译原理词法分析", "编译器的词法分析把源代码切分成 token 序列，语法分析再按文法规则构建语法树。"),
    ("doc_g13", "图形学光栅化", "光栅化把几何图元转换成屏幕像素，通过着色器计算每个像素的颜色，是实时渲染核心。"),
    ("doc_g14", "物联网设备上报", "物联网设备通过 MQTT 轻量协议上报传感器数据，云端订阅主题处理后存储分析。"),
    ("doc_g15", "嵌入式实时系统", "嵌入式实时系统要求任务在确定时间内完成，用实时操作系统 RTOS 做任务调度与中断处理。"),
    ("doc_g16", "金融风控规则", "金融风控用规则引擎与机器学习评分模型评估交易风险，命中高风险规则自动触发人工复核。"),
    ("doc_g17", "医疗影像分析", "医疗影像分析用卷积神经网络对 X 光、CT 影像做病灶检测，辅助医生诊断提高效率。"),
    ("doc_g18", "电商库存管理", "电商库存系统记录各 SKU 实时库存，下单扣减、退货回补，超卖通过预占与补偿机制避免。"),
    ("doc_g19", "物流路径规划", "物流路径规划用图算法（如 A*、Dijkstra）求最优配送路线，结合实时交通动态调整。"),
    ("doc_g20", "餐饮点餐系统", "点餐系统支持扫码下单、后厨打印、叫号取餐，高峰期用队列削峰避免系统过载。"),
    ("doc_g21", "在线教育直播", "在线教育直播用 CDN 分发音视频流，师生通过 WebRTC 低延迟互动，录制自动转存回放。"),
    ("doc_g22", "游戏服务器同步", "游戏服务器用状态同步或帧同步保证多玩家一致，关键操作做防作弊校验与回放审计。"),
    ("doc_g23", "智能交通信号", "智能交通系统按车流量自适应调节红绿灯时长，通过路口摄像头与地感线圈采集数据。"),
    ("doc_g24", "新能源充电桩", "充电桩通过 CAN/Modbus 与车辆通信，云端平台监控充电状态与电量计费，支持预约充电。"),
    ("doc_g25", "制造 MES 系统", "MES 制造执行系统实时采集产线设备数据，管理工单派发、工艺参数与质量追溯。"),
    ("doc_g26", "零售会员体系", "零售会员体系按消费积分分级，会员等级决定折扣与专属权益，积分可兑换优惠券。"),
]

# hard 检索题（干扰区分 / 信息埋藏 / 组合否定）：query + 期望关键词 + 目标文档 id
HARD_QUERIES = [
    ("Python 里不知道循环次数时，怎么反复执行直到条件满足", "while", "doc_dpy1"),
    ("给函数加计时或日志功能，除了硬编码还能用什么机制", "装饰器", "doc_02"),
    ("既要稳定又适合大数据量的排序算法选哪个", "归并排序", "doc_ds3"),
    ("按到达顺序处理任务、不插队的场景用什么数据结构", "队列", "doc_dd3"),
    ("公司采购多少钱以上必须走招投标流程", "10 万元", "doc_dc4"),
    ("App 出现重大不兼容更新时版本号该怎么升", "主版本", "doc_dp1"),
    ("员工电脑数据防泄露最基础的要求是什么", "加密", "doc_dc2"),
    ("遍历列表里每一个元素最常用的语句是什么", "for 循环", "doc_dpy2"),
    ("数据没有排序时找一个数最通用的查找方法", "线性查找", "doc_dd1"),
    ("客服遇到影响核心功能的故障先定为什么级别", "P0", "doc_dp2"),
    ("Python 报错了怎么让程序不崩溃继续往下执行", "try/except", "doc_dpy3"),
    ("给排序传一个小函数当 key 用哪种写法", "lambda", "doc_dpy4"),
    ("后进先出、适合括号匹配的是什么结构", "栈", "doc_dd2"),
    ("新员工入职头两周必须完成什么", "安全培训", "doc_dc3"),
    ("系统一年故障时间不能超过多少才算达标", "99.9%", "doc_dp3"),
    ("不能插队、严格按先后顺序处理多个任务用什么结构", "队列", "doc_dd3"),
    ("除了遍历未知长度的重复执行，条件式写法用什么", "while", "doc_dpy1"),
    ("代码出异常后想保证某些收尾代码一定执行用哪个关键字", "finally", "doc_dpy3"),
    ("打补丁版本号对应的是修什么", "修复 bug", "doc_dp1"),
    ("无序数组里顺序一个个找的方法叫什么", "线性查找", "doc_dd1"),
]


async def main():
    from milvus_client import get_milvus_client  # noqa: F401
    from postgresql_client import get_postgresql_client

    pg = await get_postgresql_client()
    milvus = await get_milvus_client()

    # 幂等：删旧库
    try:
        await milvus.delete_knowledge_file_chunks(KB_ID, USER_ID)
    except Exception:  # noqa: BLE001
        pass
    await pg.delete_knowledge_base(KB_ID, USER_ID)

    # 建库
    r = await pg.create_knowledge_base(KB_ID, USER_ID)
    print("建库:", r.get("message", r))

    # 灌入：目标文档 + 干扰文档，每篇一个父块 + 一个子块（整篇），子块走 Milvus（自动 embedding）
    # 外键约束：父块 file_hash 必须先存在 file_metadata
    chunks, parents = [], []

    def add_doc(cid: str, title: str, text: str):
        chunks.append(
            Document(
                page_content=text,
                metadata={"parent_id": cid, "file_hash": cid, "file_name": title},
            )
        )
        parents.append(
            {
                "parent_id": cid,
                "knowledge_base_id": KB_ID,
                "text": text,
                "file_name": title,
                "file_hash": cid,
            }
        )

    for cid, title, text, _q, _kw in DOCS:
        await pg.add_file_metadata(cid, title, KB_ID, USER_ID)
        add_doc(cid, title, text)
    for cid, title, text in DISTRACT_DOCS:
        await pg.add_file_metadata(cid, title, KB_ID, USER_ID)
        add_doc(cid, title, text)

    await milvus.add_chunks_batch(KB_ID, chunks, USER_ID)
    n_parents = await pg.add_parent_chunk_batch(parents, USER_ID)
    print(f"灌入完成：子块 {len(chunks)}、父块 {n_parents}（含 {len(DISTRACT_DOCS)} 篇干扰文档）")

    # 生成检索题库：easy（目标文档精确事实）+ hard（干扰区分 / 信息埋藏 / 组合否定）
    cases = [
        {
            "id": f"ret_{cid}",
            "retrieval": True,
            "kb_id": KB_ID,
            "query": q,
            "expected_keyword": kw,
            "target_doc": cid,  # 文档级判定：必须召回指定父块
            "top_k": 1,
            "desc": title,
            "difficulty": "easy",
        }
        for cid, title, _t, q, kw in DOCS
    ]
    for i, (q, kw, target) in enumerate(HARD_QUERIES, 1):
        cases.append(
            {
                "id": f"ret_hard_{i}",
                "retrieval": True,
                "kb_id": KB_ID,
                "query": q,
                "expected_keyword": kw,
                "target_doc": target,
                "top_k": 1,
                "desc": f"hard → {target}",
                "difficulty": "hard",
            }
        )
    out = os.path.join(os.path.dirname(__file__), "cases", "retrieval_cases.yaml")
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(cases, f, allow_unicode=True, sort_keys=False)
    print(f"检索题库 {len(cases)} 题（easy {len(DOCS)} + hard {len(HARD_QUERIES)}）→ retrieval_cases.yaml")


if __name__ == "__main__":
    asyncio.run(main())
