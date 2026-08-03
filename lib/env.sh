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

# 載入並驗證叢集設定檔。用法：tk_load_conf <叢集名> || exit 1
# 格式維持「被 source 的 shell 變數」（教材相容），但缺漏與格式錯誤會具名報錯，
# 不再靜默失敗
tk_load_conf() {
   local _f="${TK_HOME}/conf/${1}.conf" _err=0 _v
   if [ ! -f "${_f}" ]; then
      echo "設定檔不存在：${_f}"
      echo "可用環境：$(ls "${TK_HOME}/conf/" 2>/dev/null | sed 's/\.conf$//' | tr '\n' ' ')"
      return 1
   fi
   source "${_f}"
   for _v in TKIND NTP KVER IMG NID NGW; do
      [ -z "${!_v}" ] && echo "設定檔錯誤：${_f} 缺少 ${_v}" && _err=1
   done
   [ -z "${LCTN}${ECTN}" ] && echo "設定檔錯誤：${_f} 缺少節點清單（LCTN 或 ECTN）" && _err=1
   case "${NTP}" in Internal|External) ;; *) echo "設定檔錯誤：NTP（${NTP}）須為 Internal 或 External" && _err=1 ;; esac
   [[ "${NID}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] || { echo "設定檔錯誤：NID（${NID}）不是合法 CIDR"; _err=1; }
   [[ "${NGW}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || { echo "設定檔錯誤：NGW（${NGW}）不是合法 IPv4"; _err=1; }
   return ${_err}
}

# 節點內的 taroko 目錄（常數）：主機的 ${CLUSTERS_DIR}/<cn> 掛為節點的這個位置，
# ${WULIN_DIR} 掛為其下的 wulin/。靜態 manifest（local-path、教材 hostPath）與
# admin image 內的路徑吃不到變數、以字面值寫死——改這裡必須同步改那些字面值
export NODE_TAROKO_DIR="/opt/taroko"
