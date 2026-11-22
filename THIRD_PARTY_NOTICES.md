# 第三方资源与工程来源

本工程前端以用户提供的网站页面资源为基础，保留其 LIFT AI 布局、图标和交互，再对接新实现的本地后端。前端来源与后端实现应分开记录；本工程不代表已经获得原网站后端源代码。

`frontend/vendor/pdfjs/` 使用 Mozilla PDF.js 4.10.38。中文字符映射与标准字体支持文件来自相同版本的官方 `pdfjs-dist` npm 包。许可证保存在 `frontend/vendor/pdfjs/LICENSE`；字体目录内的许可证说明随文件一并保留。

`frontend/vendor/vanta/` 包含动态背景所需的 Three.js 0.134.0（r134）和 Vanta 0.5.24 的 WAVES / BIRDS / GLOBE / HALO / RINGS / FOG / NET 效果，均来自对应的官方 npm 发布包。两者使用 MIT 许可证，全文分别保存在 `LICENSE-three.txt` 和 `LICENSE-vanta.txt`。`asset-manifest.json` 记录发布包来源、固定版本、包完整性值和文件 SHA-256；页面运行时全部从本机加载这些文件。

Python 依赖通过各自官方发布包安装，版本记录于 `requirements.lock.txt`，各包许可证仍适用。源码压缩包不包含 Python 解释器、已安装依赖、原站点接口数据或用户的本地工作空间。

图纸 PDF 使用 [pypdfium2 5.15.2](https://pypi.org/project/pypdfium2/5.15.2/) 随官方 wheel 提供的 PDFium 在本地渲染。包内 `LICENSES/` 和 `BUILD_LICENSES/` 分别提供 Python 包及所附 PDFium 构建的许可证。

`frontend/vendor/dagre/` 使用 Dagre 3.1.3（`@dagrejs/dagre`）为扩展工作流提供本地自动排版支持，来自官方 npm 注册表发布包，使用 MIT 许可证。当前已知流程采用语义约束的紧凑布局和避障路由。主许可证及随包的第三方声明分别保存在 `LICENSE` 和 `dagre.min.js.LEGAL.txt`。`asset-manifest.json` 记录固定版本、发布包 SHA-512 完整性和本地文件 SHA-256；导出时仅从本机加载脚本，不访问远程排版服务。
