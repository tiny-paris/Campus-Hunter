package campus.monitor.tests

import org.json.JSONObject
import java.time.Instant
import java.util.TimeZone

fun main() {
    var checks = 0
    fun verify(condition: Boolean, label: String) {
        check(condition) { label }
        checks++
    }
    fun response(code: Int, message: String, status: Int = 200) =
        JSONObject().put("code", code).put("message", message).put("_http_status", status)
    fun page(ids: List<Int>, metadata: Boolean = false) = JSONObject().put("data",
        JSONObject().put("items", ids.map { JSONObject().put("id", it) }).apply {
            if (metadata) put("total", 3).put("last_page", 2)
        })
    fun rejects(label: String, block: () -> Unit) {
        val failed = try { block(); false } catch (_: IllegalStateException) { true }
        verify(failed, label)
    }

    TimeZone.setDefault(TimeZone.getTimeZone("America/Los_Angeles"))
    val instant = Instant.parse("2026-11-12T04:00:00Z").toEpochMilli()
    for (value in listOf("2026-11-12 12:00", "2026-11-12 12:00:00",
            "2026-11-12T12:00:00+08:00", "2026-11-12T04:00:00Z")) {
        verify(MonitorPolicy.signTimeMillis(value) == instant, "Time zone parsing: $value")
    }
    verify(MonitorPolicy.signTimeMillis("bad") == null, "Invalid time")
    verify(MonitorPolicy.canApply("报名中", 1200, 1800, 1100), "Server open status allows immediate submission")
    verify(!MonitorPolicy.canApply("未开始", 1200, 1800, 1100), "Upcoming registration waits")
    verify(MonitorPolicy.canApply("未开始", 1200, 1800, 1200), "Opening time triggers submission")
    verify(!MonitorPolicy.canApply("报名中", 1200, 1800, 1800), "Closed window blocks submission")
    verify(!MonitorPolicy.canApply("已结束", 1200, 1800, 1500), "Ended status blocks submission")
    verify(MonitorPolicy.outcome(response(200, "成功")) == ApplyOutcome.SUCCESS, "Success 200")
    verify(MonitorPolicy.outcome(response(0, "成功")) == ApplyOutcome.SUCCESS, "Success 0")
    verify(MonitorPolicy.outcome(response(500, "未成功")) == ApplyOutcome.RETRY, "Negative success message")
    verify(MonitorPolicy.outcome(response(400, "报名未开始")) == ApplyOutcome.RETRY, "Not open yet")
    verify(MonitorPolicy.outcome(response(500, "服务繁忙")) == ApplyOutcome.RETRY, "Busy server")
    verify(MonitorPolicy.outcome(response(200, "成功", 503)) == ApplyOutcome.RETRY, "HTTP overrides business code")
    verify(MonitorPolicy.outcome(response(401, "登录已过期")) == ApplyOutcome.AUTH_EXPIRED, "Expired token")
    verify(MonitorPolicy.outcome(response(400, "名额已满")) == ApplyOutcome.TERMINAL, "Full course")
    verify(MonitorPolicy.outcome(response(400, "重复报名")) == ApplyOutcome.TERMINAL, "Duplicate apply")
    verify(MonitorPolicy.outcome(JSONObject()) == ApplyOutcome.RETRY, "Empty response")
    verify(MonitorPolicy.outcome(JSONObject().put("message", "未成功")) == ApplyOutcome.RETRY, "Missing code failure")
    verify(MonitorPolicy.retryDelayMillis(null, 1) == 1000L, "Fast first retry")
    verify(MonitorPolicy.retryDelayMillis(null, 10) == 15_000L, "Bounded backoff")
    verify(MonitorPolicy.retryDelayMillis(response(400, "报名未开始"), 5) == 1000L, "Opening retry")
    verify(MonitorPolicy.retryDelayMillis(response(429, "频率限制", 429), 1) == 30_000L, "Rate limit backoff")
    verify(MonitorPolicy.pollDelayMillis(1000, 1040) == 40L, "Deadline precision")
    verify(MonitorPolicy.pollDelayMillis(1000, 999) == 20L, "No busy loop")
    verify(MonitorPolicy.pollDelayMillis(1000, null) == 500L, "Idle polling")

    val catalog = CourseCatalog()
    verify(!catalog.accept(page(listOf(1, 2), true)), "Needs another page")
    verify(catalog.accept(page(listOf(2, 3))), "Metadata avoids empty probe")
    verify(catalog.courses.keys.toList() == listOf("1", "2", "3"), "Ordered deduplication")
    val capped = CourseCatalog()
    verify(!capped.accept(page(listOf(1))), "Short page is not necessarily last")
    verify(!capped.accept(page(listOf(2))), "Read capped second page")
    verify(capped.accept(page(emptyList())), "Stop at confirmed empty page")
    rejects("Repeated page rejected") {
        CourseCatalog().apply { accept(page(listOf(1))); accept(page(listOf(1))) }
    }
    rejects("Missing data rejected") { CourseCatalog().accept(JSONObject()) }
    rejects("Missing ID rejected") {
        CourseCatalog().accept(JSONObject("{\"data\":[{\"title\":\"invalid\"}]}"))
    }
    rejects("Partial catalog rejected") {
        CourseCatalog().apply { accept(page(listOf(1), true)); accept(page(emptyList())) }
    }
    rejects("Expired token rejected") { CourseCatalog().accept(response(401, "过期")) }
    verify(CourseCatalog().accept(JSONObject("{\"data\":{\"items\":[]},\"total\":0,\"last_page\":0}")),
        "Empty catalog with root metadata")
    println("Android policy checks passed: $checks")
}
