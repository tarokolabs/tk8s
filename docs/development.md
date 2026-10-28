# 開發

```bash
git clone https://github.com/tarokolabs/tk8s.git && cd tk8s
uv sync                       # Python 3.14、相依套件、開發工具（pytest、ruff、mypy）
uv run pytest -q              # 單元與流程測試，全部在 FakeHost 上，數秒內完成
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
uv run tkctl create cluster --dry-run
```

開發者 clone 到任何位置後 `uv sync`，再用 `uv run tkctl …`；`tkctl` 依自身位置找到 repo（樣板、manifest、`versions.yaml`），不需要 source 任何 profile。

## 測試的規矩

- 沒有假的可執行檔、沒有 monkeypatch `subprocess`：測試用 `FakeHost`，它記下每道指令與每個寫入的檔，並回答 `systemctl is-active`、`podman … exists` 這類問題。
- 每個 phase 有全新、已完成、中斷後接續三種案例。
- `tests/golden/` 是兩種拓樸的完整 `--dry-run` 輸出，更新時要說明原因（`UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py` 重新產生）。
- README 的指令表與 phase 樹由測試固定：指令表是 `tkctl --help` 的動詞集合，phase 樹是 `--list-phases` 的逐字輸出。
- 實際主機的驗證是 `e2e` workflow：每次 push 到 `main` 與每個 tag，在 GitHub runner 上以兩個 K8s 版本各建一個叢集、離線、`verify` 全部通過、節點不下載任何 image。PR 只跑 `ci`（lint 與 pytest）；要在 PR 分支跑 e2e 用 `gh workflow run e2e.yaml --ref <分支>`。

兩條界線，越過要先討論再接受：`src/tk8s/host.py` 以外出現 `subprocess`（目前唯一的例外是 `phases.py` 查版本的 `git describe`）、樣板裡出現條件或迴圈。模組與函式的長度沒有硬性上限：一個檔或一個函式長到不好讀就拆，長度本身不是理由（`host.py` 三種 host 放一起、`units.render` 一次列完所有檔案，都是刻意的）。

## 版本

`main` 是開發線；穩定版用最新的 [Release](https://github.com/tarokolabs/tk8s/releases) tag（`TK_VERSION=<tag> sh install.sh`）。tag 的格式是 `vYYYY.M.N`。

支援政策：**最新與次新的 K8s minor**，目前為 1.37.x 與 1.36.x，固定在 `versions.yaml`（K8s patch 與 CRI-O patch 都在那裡）。CI 以這兩個版本各建一個叢集並跑 `tkctl verify cluster`。更舊的版本可以用 `--k8s` 指定：已發布的 tag 直接下載；沒發布的要 `versions.yaml` 的 `crio` 表有那個 minor 才能在本機建置，不在主要支援範圍。

`uv` 與 `kubectl` 的版本與 checksum 也在 `versions.yaml`，`install.sh` 照它下載。

## 與 v1 的差異

v1 世代（`kto`、`conf/`）的最後版本是 [v2026.9.1](https://github.com/tarokolabs/tk8s/releases/tag/v2026.9.1)，不再維護。v1 叢集不自動轉換：以 v2026.9.1 的工具備份或拆除，重建前先備份 `storage/` 裡的 PVC 資料。

- 沒有 `conf/*.conf`：拓樸用旗標或 `-f`，網段自動分配。
- 沒有 `kto`、`kls` 這些短命令，也沒有 `tkctl cluster create` 這種名詞在前的寫法。
- 不需要 source profile，不需要 `bc`、`jq`、`envsubst`、`nc`。
- 節點由 systemd 管理，主機重新開機後叢集自動啟動；v1 要手動 `kci`。
- 多 control plane 會真正 join；apiserver 入口是每叢集的 VyOS router，不是 VIP（v1 用 kube-vip）。
- 不再有 macvlan 外接節點、`tkport` DNAT 註解、`route-add`、`expose`；對外曝露改用 LoadBalancer 或 Gateway。
- 只支援 systemd 主機，Alpine 不在範圍內；節點只有 CRI-O，不再有 containerd 變體。

## 更早的世代

本 repo 曾經是 VMware Workstation + Talos Linux 世代（VMTK2024）。該世代的最後狀態保留在 tag [`pre-restructure-2026-07-29`](https://github.com/tarokolabs/tk8s/tree/pre-restructure-2026-07-29)。
