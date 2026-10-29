package com.kizitonwose.calendar.core

import androidx.compose.ui.text.intl.Locale
import com.kizitonwose.calendar.icu.UCalendarAttribute
import com.kizitonwose.calendar.icu.UCalendarType
import com.kizitonwose.calendar.icu.UErrorCode
import com.kizitonwose.calendar.icu.ucal_close
import com.kizitonwose.calendar.icu.ucal_getAttribute
import com.kizitonwose.calendar.icu.ucal_open
import com.kizitonwose.calendar.icu.uloc_forLanguageTag
import kotlinx.cinterop.ByteVar
import kotlinx.cinterop.ExperimentalForeignApi
import kotlinx.cinterop.alloc
import kotlinx.cinterop.allocArray
import kotlinx.cinterop.cstr
import kotlinx.cinterop.memScoped
import kotlinx.cinterop.ptr
import kotlinx.datetime.DayOfWeek

@OptIn(ExperimentalForeignApi::class)
public actual fun firstDayOfWeekFromLocale(locale: Locale): DayOfWeek = memScoped {
    val status = alloc<UErrorCode.Var>()
    status.value = UErrorCode.U_ZERO_ERROR
    val languageTag = locale.toLanguageTag().cstr.ptr
    // 按 ICU 的预检契约取得完整 localeID 容量；不收紧其 BCP47 部分解析语义。
    val length = uloc_forLanguageTag(languageTag, null, 0, null, status.ptr)
    check(status.value == UErrorCode.U_BUFFER_OVERFLOW_ERROR || status.value.value <= 0) {
        "ICU locale conversion failed: ${status.value}"
    }
    status.value = UErrorCode.U_ZERO_ERROR
    val localeId = allocArray<ByteVar>(length + 1)
    uloc_forLanguageTag(languageTag, localeId, length + 1, null, status.ptr)
    check(status.value.value <= 0) { "ICU locale conversion failed: ${status.value}" }
    val calendar = ucal_open(null, 0, localeId, UCalendarType.UCAL_DEFAULT, status.ptr)
    try {
        check(status.value.value <= 0 && calendar != null) { "ICU calendar open failed: ${status.value}" }
        val day = ucal_getAttribute(calendar, UCalendarAttribute.UCAL_FIRST_DAY_OF_WEEK)
        val isoDay = if (day == 1) 7 else day - 1
        DayOfWeek.entries[isoDay - 1]
    } finally {
        if (calendar != null) ucal_close(calendar)
    }
}
