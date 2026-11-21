package {{ cookiecutter.org_name_2 }}.{{ cookiecutter.package_name }}

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.FileObserver
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import io.flutter.util.PathUtils
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

class MonitorService : Service() {
    companion object {
        const val ACTION_STOP = "campus.monitor.STOP"
        private const val CHANNEL = "campus_monitor"
        private const val NEWS_CHANNEL = "campus_new_courses"
        private const val BASE_URL = "https://qcbldekt.bit.edu.cn"
        private const val TEMPLATE_ID = "2GNFjVv2S7xYnoWeIxGsJGP1Fu2zSs28R6mZI7Fc2kU"
        private const val USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 " +
            "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI " +
            "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090b13) XWEB/11275"
    }

    @Volatile private var running = false
    private var worker: Thread? = null
    private val stateChanged = AtomicBoolean()
    private val wakeWorker = Semaphore(0)
    private var stateObserver: FileObserver? = null
    private data class WatchedCourse(val course: JSONObject, val id: String, val start: Long?, val end: Long?)
    private data class ApplyResult(val watched: WatchedCourse, val response: JSONObject?, val token: String)
    private var watched = emptyList<WatchedCourse>()
    private val completed = mutableSetOf<String>()
    private val terminal = mutableSetOf<String>()
    private val retryAt = mutableMapOf<String, Long>()
    private val attempts = mutableMapOf<String, Int>()
    private val inFlight = mutableMapOf<String, Future<ApplyResult>>()
    private val applyPool = Executors.newFixedThreadPool(4)
    private val scanPool = Executors.newSingleThreadExecutor()
    private var scanFuture: Future<Boolean>? = null
    private var nextScanAt = 0L
    private var stateStamp = ""
    private var currentToken = ""
    private var tokenStamp = ""
    private var lastStatus = ""
    private var wakeLock: PowerManager.WakeLock? = null
    private val prefs by lazy { getSharedPreferences("campus_monitor", MODE_PRIVATE) }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        try {
            // Older versions also saved failed requests as completed.
            val saved = JSONArray(prefs.getString("successful_ids_v2", "[]"))
            for (i in 0 until saved.length()) completed.add(saved.optString(i))
        } catch (_: Exception) { }
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(CHANNEL, "后台活动巡检", NotificationManager.IMPORTANCE_LOW))
        manager.createNotificationChannel(NotificationChannel(NEWS_CHANNEL, "新活动提醒", NotificationManager.IMPORTANCE_DEFAULT))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            prefs.edit().putBoolean("enabled", false).apply()
            stopSelf()
            return START_NOT_STICKY
        }
        prefs.edit().putBoolean("enabled", true).apply()
        showForeground("正在启动巡检…")
        if (!running) {
            running = true
            observeState()
            worker = Thread(::monitorLoop, "campus-monitor").also { it.start() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        stateObserver?.stopWatching()
        worker?.interrupt()
        applyPool.shutdownNow()
        scanPool.shutdownNow()
        setAwake(false)
        super.onDestroy()
    }

    private fun setAwake(enabled: Boolean) {
        if (enabled) {
            if (wakeLock == null) {
                wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
                    .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "CampusHunter:Monitor")
                    .apply { setReferenceCounted(false) }
            }
            if (wakeLock?.isHeld == false) wakeLock?.acquire()
        } else if (wakeLock?.isHeld == true) {
            wakeLock?.release()
        }
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 1, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val stop = PendingIntent.getService(
            this, 2, Intent(this, MonitorService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("第二课堂活动巡检")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "停止巡检", stop).build())
            .build()
    }

    private fun showForeground(text: String) {
        if (text == lastStatus) return
        lastStatus = text
        val note = notification(text)
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1001, note, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1001, note)
        }
    }

    // Flutter's path_provider uses the same directory for FLET_APP_STORAGE_DATA.
    private fun stateDirectory() = File(PathUtils.getDataDirectory(this))

    @Suppress("DEPRECATION") // The string constructor also supports Android 8 and 9.
    private fun observeState() {
        stateObserver = object : FileObserver(stateDirectory().path, FileObserver.MOVED_TO or FileObserver.CLOSE_WRITE) {
            override fun onEvent(event: Int, path: String?) {
                if (path == "app_state.json" || path == "token.txt") {
                    if (!stateChanged.getAndSet(true)) wakeWorker.release()
                }
            }
        }
        stateObserver?.startWatching()
    }

    private fun monitorLoop() {
        val directory = stateDirectory()
        val stateFile = File(directory, "app_state.json")
        val tokenFile = File(directory, "token.txt")
        while (running) {
            try {
                val changed = stateChanged.getAndSet(false)
                var delay = 500L
                val stamp = "${tokenFile.lastModified()}:${tokenFile.length()}"
                if (stamp != tokenStamp) {
                    currentToken = tokenFile.takeIf { it.isFile }?.readText(Charsets.UTF_8)?.trim().orEmpty()
                    tokenStamp = stamp
                    retryAt.clear()
                    attempts.clear()
                }
                drainApplyResults(directory)
                val token = currentToken
                if (!token.startsWith("Bearer ", ignoreCase = true)) {
                    setAwake(false)
                    showForeground("等待 Token 配置")
                } else {
                    setAwake(true)
                    readWatched(stateFile, changed)
                    val now = System.currentTimeMillis()
                    applyReadyCourses(token, now)
                    showForeground("监控 ${watched.count { it.id !in completed && it.id !in terminal }} 项 · 每 5 分钟巡检")
                    val elapsed = SystemClock.elapsedRealtime()
                    val scan = scanFuture
                    if (scan != null && scan.isDone) {
                        val ok = try { scan.get() } catch (_: Exception) { false }
                        nextScanAt = elapsed + if (ok) 300_000L else 30_000L
                        scanFuture = null
                    }
                    if (scanFuture == null && elapsed >= nextScanAt) {
                        scanFuture = scanPool.submit<Boolean> { scanNewCourses(token) }
                    }
                    val deadline = watched.asSequence()
                        .filter { it.id !in completed && it.id !in terminal && it.id !in inFlight }
                        .map { maxOf(it.start ?: now, now + ((retryAt[it.id] ?: elapsed) - elapsed)) }
                        .minOrNull()
                    delay = MonitorPolicy.pollDelayMillis(now, deadline)
                    if (inFlight.isNotEmpty()) delay = minOf(delay, 50L)
                }
                wakeWorker.tryAcquire(delay, TimeUnit.MILLISECONDS)
            } catch (_: InterruptedException) {
                break
            } catch (e: Exception) {
                showForeground("巡检暂时失败，稍后重试")
                try { wakeWorker.tryAcquire(5000L, TimeUnit.MILLISECONDS) } catch (_: InterruptedException) { break }
            }
        }
    }

    private fun readWatched(file: File, changed: Boolean) {
        if (!file.isFile) return
        val stamp = "${file.lastModified()}:${file.length()}"
        if (!changed && stamp == stateStamp) return
        val next = JSONObject(file.readText(Charsets.UTF_8)).optJSONArray("watched_courses") ?: JSONArray()
        val snapshots = mutableListOf<WatchedCourse>()
        val ids = mutableSetOf<String>()
        for (i in 0 until next.length()) {
            val course = next.optJSONObject(i) ?: continue
            val id = course.optString("id").toIntOrNull()?.takeIf { it > 0 }?.toString() ?: continue
            if (!ids.add(id)) continue
            snapshots.add(WatchedCourse(course, id,
                MonitorPolicy.signTimeMillis(course.optString("sign_start_time")),
                MonitorPolicy.signTimeMillis(course.optString("sign_end_time"))))
        }
        if (completed.retainAll(ids)) persistCompleted()
        terminal.retainAll(ids)
        retryAt.keys.retainAll(ids)
        attempts.keys.retainAll(ids)
        watched = snapshots.sortedBy { it.start ?: Long.MIN_VALUE }
        stateStamp = stamp
    }

    private fun persistCompleted() {
        prefs.edit().putString("successful_ids_v2", JSONArray(completed.toList()).toString()).apply()
    }

    private fun applyReadyCourses(token: String, now: Long) {
        for (entry in watched) {
            val id = entry.id
            if (id in completed || id in terminal || id in inFlight) continue
            if ((entry.end != null && now >= entry.end) || entry.course.optString("sign_status_label") == "已结束") {
                terminal.add(id)
                continue
            }
            if (SystemClock.elapsedRealtime() < (retryAt[id] ?: 0L)) continue
            if (!MonitorPolicy.canApply(entry.course.optString("sign_status_label"), entry.start, entry.end, now)) continue
            if (inFlight.size >= 4) break
            inFlight[id] = applyPool.submit<ApplyResult> {
                val payload = JSONObject().put("course_id", id.toInt()).put("template_id", TEMPLATE_ID)
                ApplyResult(entry, request("/api/course/apply", token, payload), token)
            }
        }
    }

    private fun drainApplyResults(directory: File) {
        val finished = inFlight.filterValues { it.isDone }.keys.toList()
        for (id in finished) {
            val future = inFlight.getValue(id)
            // Keep the completed future until its result has been written successfully.
            val event = try { future.get() } catch (_: Exception) {
                val entry = watched.firstOrNull { it.id == id }
                if (entry == null) {
                    inFlight.remove(id)
                    continue
                }
                ApplyResult(entry, null, currentToken)
            }
            val course = event.watched.course
            val result = event.response
            val outcome = MonitorPolicy.outcome(result)
            val message = result?.optString("message", result.optString("msg")).orEmpty()
            if (event.token != currentToken && outcome != ApplyOutcome.SUCCESS) {
                inFlight.remove(id)
                continue
            }
            if (outcome == ApplyOutcome.SUCCESS) {
                val event = JSONObject()
                    .put("id", course.optInt("id"))
                    .put("title", course.optString("title", "未知活动"))
                    .put("time_place", course.optString("time_place", "时间地点待定"))
                    .put("score", course.opt("score"))
                    .put("captured_at", LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")))
                    .put("message", message)
                File(directory, "mobile_results.jsonl")
                    .appendText(event.toString() + "\n", Charsets.UTF_8)
                completed.add(id)
                persistCompleted()
                notifyNews("报名成功", course.optString("title", "活动报名成功"), 3000 + course.optInt("id"))
            } else if (outcome == ApplyOutcome.TERMINAL) {
                val event = JSONObject().put("id", id.toInt()).put("outcome", "terminal")
                    .put("title", course.optString("title", "未知活动")).put("message", message)
                File(directory, "mobile_results.jsonl").appendText(event.toString() + "\n", Charsets.UTF_8)
                terminal.add(id)
                notifyNews("报名已停止", "${course.optString("title")}：$message", 3000 + course.optInt("id"))
            } else {
                val attempt = (attempts[id] ?: 0) + 1
                attempts[id] = attempt
                retryAt[id] = SystemClock.elapsedRealtime() + MonitorPolicy.retryDelayMillis(result, attempt)
            }
            inFlight.remove(id)
        }
    }

    private fun scanNewCourses(token: String): Boolean {
        val catalog = CourseCatalog()
        try {
            while (running && !Thread.currentThread().isInterrupted) {
                val response = request("/api/course/list?page=${catalog.page + 1}&limit=100", token) ?: return false
                if (catalog.accept(response)) break
            }
            if (!running || Thread.currentThread().isInterrupted) return false
        } catch (_: Exception) { return false }
        val seenRaw = prefs.getString("catalog_seen_ids_v2", null)
        val seen = mutableSetOf<String>()
        if (seenRaw != null) {
            val array = JSONArray(seenRaw)
            for (i in 0 until array.length()) seen.add(array.optString(i))
        }
        val isFirstScan = seenRaw == null
        for ((id, course) in catalog.courses) {
            if (!isFirstScan && id !in seen && course.optString("sign_status_label") != "已结束") {
                notifyNews("发现新活动", course.optString("title", "新活动"), 5000 + course.optInt("id"))
            }
            seen.add(id)
        }
        prefs.edit().putString("catalog_seen_ids_v2", JSONArray(seen.toList().takeLast(2000)).toString()).apply()
        return true
    }

    private fun notifyNews(title: String, body: String, id: Int) {
        val manager = getSystemService(NotificationManager::class.java)
        if (!manager.areNotificationsEnabled()) return
        val open = PendingIntent.getActivity(
            this, id, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val note = Notification.Builder(this, NEWS_CHANNEL)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(title)
            .setContentText(body)
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        manager.notify(id, note)
    }

    private fun request(endpoint: String, token: String, payload: JSONObject? = null): JSONObject? {
        val connection = URL(BASE_URL + endpoint).openConnection() as HttpURLConnection
        try {
            connection.connectTimeout = 3000
            connection.readTimeout = if (payload == null) 10_000 else 5000
            connection.requestMethod = if (payload == null) "GET" else "POST"
            connection.instanceFollowRedirects = false
            connection.setRequestProperty("Authorization", token)
            connection.setRequestProperty("User-Agent", USER_AGENT)
            connection.setRequestProperty("Referer", "https://servicewechat.com/wx2149021480f24a19/270/page-frame.html")
            connection.setRequestProperty("Content-Type", "application/json")
            if (payload != null) {
                connection.doOutput = true
                connection.outputStream.use { it.write(payload.toString().toByteArray(Charsets.UTF_8)) }
            }
            val status = connection.responseCode
            val body = (if (status < 400) connection.inputStream else connection.errorStream)
                ?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: return null
            return JSONObject(body).put("_http_status", status)
        } catch (_: Exception) {
            connection.disconnect()
            return null
        }
    }
}

class MonitorBootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        if (intent?.action != Intent.ACTION_BOOT_COMPLETED) return
        if (!context.getSharedPreferences("campus_monitor", Context.MODE_PRIVATE)
                .getBoolean("enabled", false)) return
        try {
            val service = Intent(context, MonitorService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(service)
            else context.startService(service)
        } catch (_: Exception) {
            // The user can restart monitoring by opening the app.
        }
    }
}
