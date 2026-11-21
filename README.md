# Campus Hunter · Android

北京理工大学第二课堂的 Android 活动助手。可以浏览和筛选活动、查看个人积分，并把活动加入监控，在报名开放后尝试提交报名。

[下载最新 APK](https://github.com/tiny-paris/Campus-Hunter/releases/latest)

## 安装与使用

当前安装包支持 **Android 8.0 及以上（ARM64）**。在 Releases 中下载 `.apk`，安装后打开应用，填入本人有效的第二课堂 Bearer Token。凭证保存在手机的应用私有目录，可在「我的 → 设置 → 登录凭证」更新。

- **活动**可手动刷新和按类别筛选。发现新活动后，点「近期上新」查看。若后续分页加载失败，已读取的数据仍会显示，并标为「部分更新」。
- **监控**汇总已加入的活动。报名开放时加入监控会立即尝试报名，尚未开放的活动会等待开始时间。
- **我的**显示账户、积分、报名成功记录和运行日志。

后台巡检通过 Android 前台服务运行，每 5 分钟检查新活动。允许通知后可通过常驻通知查看巡检状态或停止。锁屏时的运行情况取决于手机的后台和电池策略。

如果活动列表能加载，但账户和积分同时为空，可在第二课堂小程序确认登录状态。服务端若返回业务码 `10001`，请更新登录凭证。

## 从源码构建

使用 Python 3.12 或 3.13、Flutter 3.29.2、JDK 17、Android SDK 35 和 Build Tools 34。首次构建前需接受 Android SDK 许可协议。

在仓库根目录运行：

```powershell
python -m pip install "flet[all]==0.28.3" "requests==2.32.5"
flet build apk . --arch arm64-v8a
```

构建结果位于 `build/apk/`。`src/` 是 Flet 界面和前台请求逻辑，`template_source/` 包含 Android 前台服务和打包模板。

运行离线回归测试：

```powershell
python -m unittest discover -s tests -v
```

[MIT 许可证](LICENSE)
