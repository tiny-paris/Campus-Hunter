package {{ cookiecutter.org_name_2 }}.{{ cookiecutter.package_name }}

import org.json.JSONArray
import org.json.JSONObject
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId

internal enum class ApplyOutcome { SUCCESS, TERMINAL, RETRY, AUTH_EXPIRED }

internal object MonitorPolicy {
    fun canApply(status: String, start: Long?, end: Long?, now: Long): Boolean =
        status != "已结束" && (end == null || now < end) &&
            (status == "报名中" || start == null || now >= start)

    fun signTimeMillis(value: String): Long? {
        if (value.isBlank()) return null
        val iso = value.trim().replace(' ', 'T')
        return try {
            OffsetDateTime.parse(iso).toInstant().toEpochMilli()
        } catch (_: Exception) {
            try {
                LocalDateTime.parse(iso).atZone(ZoneId.of("Asia/Shanghai"))
                    .toInstant().toEpochMilli()
            } catch (_: Exception) { null }
        }
    }

    fun outcome(result: JSONObject?): ApplyOutcome {
        if (result == null) return ApplyOutcome.RETRY
        val status = result.optInt("_http_status", 200)
        val code = result.optString("code")
        val message = result.optString("message", result.optString("msg"))
        if (status == 401 || code == "401" || message.contains("Unauthenticated", true) ||
            message.contains("登录已过期") || message.contains("Token", true)) {
            return ApplyOutcome.AUTH_EXPIRED
        }
        if (status !in 200..299) return ApplyOutcome.RETRY
        if (code == "0" || code == "200" ||
            (code.isEmpty() && message.trim().trimEnd('!', '！', '。') in
                setOf("成功", "报名成功", "申请成功", "操作成功"))) return ApplyOutcome.SUCCESS
        if (listOf("名额已满", "人数已满", "报名已结束", "报名已截止", "活动已结束",
                "不符合报名条件", "无报名资格", "不在报名范围", "已报名", "重复报名")
                .any { message.contains(it) }) return ApplyOutcome.TERMINAL
        return ApplyOutcome.RETRY
    }

    fun retryDelayMillis(result: JSONObject?, attempt: Int): Long {
        if (outcome(result) == ApplyOutcome.AUTH_EXPIRED) return 60_000L
        if (result?.optInt("_http_status") == 429 || result?.optString("code") == "429") return 30_000L
        val message = result?.optString("message", result.optString("msg")).orEmpty()
        if (listOf("未开始", "未到报名", "尚未开放").any { message.contains(it) }) return 1000L
        return (1000L shl (attempt - 1).coerceIn(0, 4)).coerceAtMost(15_000L)
    }

    fun pollDelayMillis(now: Long, deadline: Long?): Long =
        if (deadline == null) 500L else (deadline - now).coerceIn(20L, 500L)
}

/** Keep partial/invalid scans out of the notification baseline. */
internal class CourseCatalog {
    val courses = linkedMapOf<String, JSONObject>()
    var page = 0
        private set
    private var total: Int? = null
    private var lastPage: Int? = null

    fun accept(response: JSONObject): Boolean {
        check(response.optInt("_http_status", 200) in 200..299) { "活动列表请求失败" }
        check(!response.has("code") || response.optString("code") in setOf("0", "200")) { "活动列表加载失败" }
        val data = response.opt("data")
        val items = if (data is JSONObject) data.optJSONArray("items") else data as? JSONArray
        check(items != null) { "活动列表格式异常" }
        page++
        if (page == 1) {
            fun metadataInt(key: String): Int? = (data as? JSONObject)?.optString(key)?.toIntOrNull()
                ?: response.optString(key).toIntOrNull()
            total = metadataInt("total")
            lastPage = metadataInt("last_page")
            val perPage = metadataInt("per_page")
            if (lastPage == 0 && items.length() == 0 && (total == null || total == 0)) lastPage = 1
            if (lastPage == null && total != null && perPage != null && perPage > 0) {
                lastPage = maxOf(1, (total!! + perPage - 1) / perPage)
            }
            check(lastPage == null || lastPage!! in 1..200) { "活动分页信息异常" }
        }
        val before = courses.size
        for (i in 0 until items.length()) {
            val course = items.optJSONObject(i)
            val id = course?.optString("id")?.toIntOrNull()
            check(id != null && id > 0) { "活动编号格式异常" }
            courses[id.toString()] = course
        }
        check(items.length() == 0 || courses.size > before) { "活动接口返回重复分页" }
        val finished = items.length() == 0 || (lastPage != null && page >= lastPage!!)
        if (finished) {
            check(total == null || total == courses.size) { "活动分页数据不完整" }
            check(lastPage == null || page >= lastPage!!) { "活动分页提前结束" }
        } else {
            check(page < 200) { "活动分页超过读取上限" }
        }
        return finished
    }
}
