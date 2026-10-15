# 与视频一起发布

目标仓库：`GaryFigno/codex-image-queue`；首版 `v0.1.0`。这份说明不代表远端已经创建或公开。

复核最终视频与仓库，运行 `python -m unittest discover -s tests -v`。仓库只包含工具、Skill、虚构示例和测试，不包含生产工单、游戏美术、账单或凭证。

已登录 GitHub CLI 后，可先建立私有仓库上传候选版：

```sh
gh repo create GaryFigno/codex-image-queue --private --source . --remote origin --push
```

视频正式发布时，再公开仓库并建立 Release：

```sh
gh repo edit GaryFigno/codex-image-queue --visibility public --accept-visibility-change-consequences
gh release create v0.1.0 --repo GaryFigno/codex-image-queue --title "v0.1.0 · JSON 工单与多窗口出图队列" --notes-file RELEASE_NOTES.md
```

同名仓库若已存在，先核对内容，不要覆盖。远端 Actions 的 Windows/Linux 测试结果以实际运行结果为准。

仓库实际公开后，视频简介可放：

> 配套工具：https://github.com/GaryFigno/codex-image-queue 。包含 Skill、JSON 工单示例与队列程序。多窗口独立领单、失败留档、限次恢复；图片效果仍需看图验收。
