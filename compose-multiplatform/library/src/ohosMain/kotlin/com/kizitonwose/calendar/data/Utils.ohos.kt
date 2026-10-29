package com.kizitonwose.calendar.data

import kotlin.experimental.ExperimentalNativeApi
import kotlin.native.Platform
import kotlinx.cinterop.ExperimentalForeignApi
import platform.PerformanceAnalysisKit.HiLog.LOG_APP
import platform.PerformanceAnalysisKit.HiLog.LOG_WARN
import platform.PerformanceAnalysisKit.HiLog.OH_LOG_Print

@OptIn(ExperimentalNativeApi::class, ExperimentalForeignApi::class)
internal actual fun log(tag: String, message: String) {
    if (Platform.isDebugBinary) {
        OH_LOG_Print(LOG_APP, LOG_WARN, 0u, tag, "%{public}s", message)
    }
}
