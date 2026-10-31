"""qqlife._paths：唯一授权项目根解析（避免各处 parents 漂移）。"""
from pathlib import Path

# 仓库根 = agent/（本文件 src/qqlife/_paths.py → parents[0]=qqlife, [1]=src, [2]=agent）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 运行期目录（仓库根下，gitignore）。人设/机械提示/课程模板等 per-uid 内容
# 全部在 data/users/<uid>/ 家目录下（自包含迁移），不再有独立 personas 目录。
DATA_DIR = PROJECT_ROOT / "data"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
LOGS_DIR = PROJECT_ROOT / "logs"
DEBUG_DIR = PROJECT_ROOT / "debug"


def user_home(uid: str) -> Path:
    """uid 家目录：data/<uid>/ —— 一个人全部信息（人设/文案/模板/身份端点）+ 运行态。

    身份中心方案 §4.3：归属聚合且自包含迁移，拷贝 data/<uid>/ 即完成整体迁移。
    data/ 下不再有 users/ 中间层；uid 受格式约束（见 platform/identity.uid_valid），
    与任何全局文件名天然不冲突。首次访问自动创建。
    """
    home = DATA_DIR / (uid or "me")   # 空 uid 兜底 = MASTER_UID 缺省（"local" 已弃用，避免与历史目录混淆）
    home.mkdir(parents=True, exist_ok=True)
    return home