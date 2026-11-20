# MyDrive 独立原生多端客户端（Flutter + libmpv 硬解）

支持 **Android / iOS / Windows / macOS / Linux**，直接对接 MyDrive 商业版服务端：

- 登录 / 注册与自定义服务器地址
- 目录浏览、跨目录全局搜索、智能分类（视频 / 图片 / 音频 / 文档 / 收藏）
- 文件上传（支持秒传预检）与 S3/R2 预签名直链下载
- **原生 `media_kit`（libmpv）硬件解码播放器**：无需服务端转码即可原画硬解播放 4K MKV / HEVC，支持倍速（0.5x–2.0x）、外挂/内封 WebVTT 字幕挂载、续播进度同步
- 磁力 / HTTP 批量离线下载（命中全站秒存提示）与 VIP 会员兑换中心

## 快速编译与运行

```bash
cd client_flutter
flutter create . --platforms=android,ios,windows,macos,linux
flutter pub get

# 本地调试运行
flutter run

# 打包各平台安装包
flutter build apk --release        # Android APK
flutter build ipa                  # iOS (需 macOS + Apple 开发者证书)
flutter build windows --release    # Windows EXE
flutter build macos --release      # macOS APP
```
