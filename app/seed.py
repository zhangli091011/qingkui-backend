from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EdgeType, KnowledgeEdge, KnowledgeNode, KnowledgeSource


SOURCE_ID = "source-qingkui-demo-math-v1"


NODES = [
    {
        "id": "function_concept",
        "name": "函数概念",
        "chapter": "函数的概念与性质",
        "definition": "设 A、B 是非空数集，如果对 A 中任意一个数 x，按照某种确定的对应关系，在 B 中都有唯一确定的数 y 和它对应，那么称 y 是 x 的函数。",
        "explanation": "理解函数要抓住定义域、对应关系和值域三个要素，其中“每个输入有唯一输出”最关键。",
        "common_errors": ["忽略定义域", "把一对多关系误认为函数"],
        "question_types": ["判断对应关系是否为函数", "求定义域"],
        "source_excerpt": "第 1 章 · 函数基本概念",
    },
    {
        "id": "linear_function",
        "name": "一次函数",
        "chapter": "函数模型",
        "definition": "形如 y=kx+b（k≠0）的函数称为一次函数。",
        "explanation": "一次函数图像是一条直线，k 决定增减性和倾斜程度，b 决定与 y 轴的交点。",
        "common_errors": ["漏写 k≠0", "混淆斜率与截距"],
        "question_types": ["求解析式", "判断单调性"],
        "source_excerpt": "第 1 章 · 常见函数模型",
    },
    {
        "id": "quadratic_function",
        "name": "二次函数",
        "chapter": "函数的应用",
        "definition": "一般地，形如 y=ax²+bx+c（a≠0）的函数叫作二次函数。",
        "explanation": "二次函数的图像是抛物线。a 的符号决定开口方向，顶点和对称轴决定图像的位置。",
        "common_errors": ["忽略 a≠0", "配方时常数项处理错误"],
        "question_types": ["求最值", "图像与参数", "零点问题"],
        "source_excerpt": "第 2 章 · 二次函数",
    },
    {
        "id": "parabola",
        "name": "抛物线",
        "chapter": "函数的应用",
        "definition": "二次函数 y=ax²+bx+c（a≠0）的图像是一条抛物线。",
        "explanation": "抛物线关于直线 x=-b/(2a) 对称，顶点横坐标为 -b/(2a)。",
        "common_errors": ["对称轴符号写反", "把开口大小只归因于 a 的正负"],
        "question_types": ["求顶点", "画函数图像"],
        "source_excerpt": "第 2 章 · 二次函数图像",
    },
    {
        "id": "discriminant",
        "name": "判别式",
        "chapter": "一元二次方程",
        "definition": "一元二次方程 ax²+bx+c=0（a≠0）的判别式是 Δ=b²-4ac。",
        "explanation": "Δ>0 时有两个不相等实根，Δ=0 时有两个相等实根，Δ<0 时在实数范围内无根。",
        "common_errors": ["漏掉括号导致符号错误", "把 Δ=0 判断为无根"],
        "question_types": ["判断根的个数", "参数取值范围"],
        "source_excerpt": "第 2 章 · 方程与函数零点",
    },
    {
        "id": "quadratic_inequality",
        "name": "一元二次不等式",
        "chapter": "一元二次不等式",
        "definition": "只含有一个未知数，并且未知数的最高次数是 2 的整式不等式，称为一元二次不等式。",
        "explanation": "求解时可结合对应二次函数图像、零点和开口方向判断函数值的正负区间。",
        "common_errors": ["忽略二次项系数符号", "端点开闭判断错误"],
        "question_types": ["解不等式", "恒成立问题"],
        "source_excerpt": "第 2 章 · 不等式",
    },
]


EDGES = [
    ("function_concept", "quadratic_function", EdgeType.prerequisite, "函数概念是理解二次函数的基础。"),
    ("linear_function", "quadratic_function", EdgeType.related, "可通过一次与二次函数比较理解函数图像。"),
    ("quadratic_function", "parabola", EdgeType.related, "二次函数的图像是抛物线。"),
    ("quadratic_function", "discriminant", EdgeType.related, "判别式连接方程根与二次函数零点。"),
    ("quadratic_function", "quadratic_inequality", EdgeType.extension, "二次函数图像可用于求解一元二次不等式。"),
    ("discriminant", "quadratic_inequality", EdgeType.prerequisite, "根的位置决定不等式解集的端点。"),
]


def seed_demo_content(db: Session) -> None:
    if db.scalar(select(KnowledgeSource.id).where(KnowledgeSource.id == SOURCE_ID)) is None:
        db.add(
            KnowledgeSource(
                id=SOURCE_ID,
                title="青葵计划内部演示知识纲要",
                publisher="青葵计划",
                edition="V1",
                location="高一数学演示范围",
                authorization_status="self_owned_demo",
            )
        )
        db.flush()
    for item in NODES:
        if db.get(KnowledgeNode, item["id"]) is None:
            db.add(
                KnowledgeNode(
                    **item,
                    subject="数学",
                    grade="高一",
                    textbook_version="通用演示版",
                    source_id=SOURCE_ID,
                )
            )
    db.flush()
    for source_id, target_id, edge_type, explanation in EDGES:
        exists = db.scalar(
            select(KnowledgeEdge.id).where(
                KnowledgeEdge.source_node_id == source_id,
                KnowledgeEdge.target_node_id == target_id,
                KnowledgeEdge.edge_type == edge_type,
            )
        )
        if exists is None:
            db.add(
                KnowledgeEdge(
                    source_node_id=source_id,
                    target_node_id=target_id,
                    edge_type=edge_type,
                    explanation=explanation,
                )
            )
    db.commit()
