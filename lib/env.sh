# Taroko 平台的路徑定義——所有 CLI 指令 source 這一份，路徑常數只在這裡出現
# 覆寫順序：環境變數 > 預設值
#
# 使用方式（CLI 指令開頭）：
#   source "$(dirname "$(realpath "$0")")/../lib/env.sh"

# 平台安裝位置：依本檔案的實際位置自我定位——repo 可以 clone 到任何地方
# 需要顯式指定時（例如多份安裝並存）以 TK_HOME 覆寫
TK_HOME="${TK_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export TK_HOME

# 主機側狀態根目錄（FHS：/opt/<vendor>），叢集狀態、儲存與工具下載都收斂於此
export TAROKO_HOME="${TAROKO_HOME:-/opt/taroko}"

# 教材 repo（wulin）的位置；kcn 會把它掛進每個節點
export WULIN_DIR="${WULIN_DIR:-${TAROKO_HOME}/wulin}"

# 衍生路徑（主機側）
export CLUSTERS_DIR="${TAROKO_HOME}/clusters"   # 叢集狀態：${CLUSTERS_DIR}/<cn>/
export CNI_DIR="${TAROKO_HOME}/cni"             # CNI plugin 下載

# 節點內的 taroko 目錄（常數）：主機的 ${CLUSTERS_DIR}/<cn> 掛為節點的這個位置，
# ${WULIN_DIR} 掛為其下的 wulin/。靜態 manifest（local-path、教材 hostPath）與
# admin image 內的路徑吃不到變數、以字面值寫死——改這裡必須同步改那些字面值
export NODE_TAROKO_DIR="/opt/taroko"
