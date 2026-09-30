"""测试共用配置。

把仓库根目录加进 sys.path，让 `import kuaitou` 在 CI 与本地都能直接工作
（pytest 默认只把测试文件所在目录加进路径，包在上一层，所以要手动补一条）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
