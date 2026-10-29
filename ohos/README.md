# Calendar 2.10.1 OHOS 独立 producer 候选

上游 [kizitonwose/Calendar 2.10.1](https://github.com/kizitonwose/Calendar/commit/0e94f40b5849d92a6f1390b9e39a0d316c7d5408) 的 MIT 许可证、原 POM、GMM、sources JAR 和构建声明随配方保留。完整 41 份上游 common 源、两份已审查 OHOS actual 与 `calendar_icu.def` 均保留；不按调用点裁减 API，也不修改上游 Android/iOS 构建。

Prepare 从指定 Fork 提交读取 Git blob，将 41 份 common 源与固定 sources JAR 逐字节核对；两份 actual 和 cinterop 定义也须与锁定的原适配字节一致，并从同一提交复制到隔离工作区后编译。Windows 的 `core.autocrlf` 可能改变工作树换行，工作树字节不作为 Git/JAR 原始字节一致的证据。旧 JAR 中两份资源生成源码保留为来源资料，不作为编译输入；Compose Resources 插件在工作区正式生成对应 package 的源码。固定上游没有 `commonMain/composeResources` 资源资产，不声称打包了上游资源资产。

独立根坐标遵循 `io.github.qsxs.cpf.calendar:compose-multiplatform:2.10.1-cpf11-preview-<sourceSHA12>-<runID>-<attempt>`。固定 Kotlin/Native、KGP、Compose 与 serialization compiler `2.4.20-0.1.0-08`，Compose `1.12.0-0.1.0-11`、Gradle 9.5.0 和 JDK 17。上游 datetime 的 common `api`、serialization 的 common `implementation` 和 OHOS `api` 保持不变；原 `androidx.annotation` 按既有 P5-CALENDAR-AUTO-01 决策映射到 CPF annotation，仍为 `implementation`。

可先执行只读 Plan；`SOURCE_SHA` 为当前 Fork 的完整 40 位提交：

```sh
python -B ohos/CalendarPort.py Plan --source-sha "$SOURCE_SHA" --run-id "$RUN_ID" --run-attempt "$RUN_ATTEMPT" --work "$WORK_DIR" --repository "$STAGING_DIR"
```

Plan 不创建工作目录或下载工具；若两份 actual 和 def 尚未提交，只核对其当前工作树锁定字节并标为 `UNCOMMITTED_WORKTREE_ONLY`，不能视为提交身份核验。Prepare 及后续阶段要求同一 source SHA 已包含全部配方、actual 和 def，逐字节核对 Git blob 和配方身份。显式指定 work/staging、sysroot、sysroot kind，按 `Prepare`、`Tasks`、`Resolve`、`Metadata`、`NativeX64`、`NativeArm64`、`Publish`、`Verify` 顺序执行；编译阶段还需传入 `--native-home`。共享工具 A 固定为 `d0cff88fd1ea2e4f08bb4409f1c802cca0c7f285`，可通过 `--tooling` 指定该工具仓，省略时获取并核对锁定归档文件。

work 和 staging 必须相互分离、位于 Fork checkout 外，且与 sysroot 不重叠；首次 Prepare 时两者必须不存在或为空。Gradle、Kotlin/Native 和临时目录隔离在 work 内；重复 Prepare 只复核已准备身份，阶段日志独占创建。失败现场不自动清理，重试使用新的工作目录、staging 和运行标识，避免覆盖已有文件。

按用户选择，CPF `.08` sysroot `26.0.0.38-01` 是外部构建输入，以 `--sysroot <路径> --sysroot-kind cpf` 指定。CLI 核对三个所选 ICU 头文件和两个 ABI 的 `libicu.so` 身份，归档 URL/大小/SHA 是锁定值，CLI 本身不读取或验证归档；独立工作流若下载归档须另行校验。`libicu.so` 不进入仓库或发布制品，cinterop 保留系统 `-licu`。

原 DevEco SDK `26.0.0.105` 入口仍可通过 `--sysroot <native/sysroot路径> --sysroot-kind deveco` 显式选择，可选 `--sdk-package <native/oh-uni-package.json>` 记录该文件身份；此入口不自动发现 SDK，也不独立证明调用者所选 SDK 的版本。候选与原 SDK 已比较所用部分头文件和符号，但 ICU 库文件字节不同，完整 ABI 与运行等价性尚未建立。

配方检查标准 metadata、OHOS x64 和 arm64 的编译源、正式资源生成任务、双 ABI cinterop、POM/GMM、sources、许可与 staging 字节；双 Native 的 metadata 签名另外核对 17 个代表性日历 API 名称。完整源码参与编译不等于每个 API 均已逐项验证或运行。当前独立候选尚未执行 Gradle/Native、公开消费、link、runtime、设备或 Android/iOS 回归；历史 CAL-R1 共享周手势问题仍延期，历史 x64 局部运行不代表新仓通过。
