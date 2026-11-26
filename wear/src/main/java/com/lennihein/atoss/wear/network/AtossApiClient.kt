package com.lennihein.atoss.wear.network

import android.content.Context
import android.os.Build
import com.lennihein.atoss.wear.data.PendingPunch
import com.lennihein.atoss.wear.data.TokenStore
import com.lennihein.atoss.wear.data.WatchStatus
import com.lennihein.atoss.wear.tile.PunchTileService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.util.UUID
import java.util.concurrent.TimeUnit

data class PairResult(
    val token: String,
    val displayName: String,
    val timezone: String
)

class ApiAuthException(message: String) : IOException(message)

class HttpPunchException(val code: Int, message: String) : IOException(message)

object AtossApiClient {

    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()
    private val flushMutex = Mutex()
    private val queueScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(12, TimeUnit.SECONDS)
        .writeTimeout(12, TimeUnit.SECONDS)
        .build()

    suspend fun pair(
        serverUrl: String,
        code: String,
        deviceName: String = Build.MODEL ?: "Wear OS Watch"
    ): PairResult = withContext(Dispatchers.IO) {
        val base = serverUrl.trim().trimEnd('/')
        val payload = JSONObject()
            .put("code", code.trim())
            .put("device_name", deviceName)
            .toString()

        val req = Request.Builder()
            .url("$base/api/watch/pair")
            .post(payload.toRequestBody(jsonMediaType))
            .build()

        client.newCall(req).execute().use { resp ->
            val bodyStr = resp.body?.string() ?: ""
            if (!resp.isSuccessful) {
                val msg = try {
                    JSONObject(bodyStr).optString("detail", "HTTP ${resp.code}")
                } catch (_: Exception) {
                    "HTTP ${resp.code}"
                }
                throw IOException("Pairing failed: $msg")
            }
            val obj = JSONObject(bodyStr)
            val userObj = obj.getJSONObject("user")
            PairResult(
                token = obj.getString("token"),
                displayName = userObj.optString("display_name", "User"),
                timezone = userObj.optString("timezone", "Europe/Berlin")
            )
        }
    }

    suspend fun getStatus(
        serverUrl: String,
        token: String
    ): WatchStatus = withContext(Dispatchers.IO) {
        val base = serverUrl.trim().trimEnd('/')
        val req = Request.Builder()
            .url("$base/api/watch/status")
            .header("Authorization", "Bearer $token")
            .get()
            .build()

        client.newCall(req).execute().use { resp ->
            val bodyStr = resp.body?.string() ?: ""
            if (resp.code == 401) {
                throw ApiAuthException("Token rejected (401)")
            }
            if (!resp.isSuccessful) {
                throw IOException("Status failed: HTTP ${resp.code}")
            }
            WatchStatus.fromJsonString(bodyStr)
                ?: throw IOException("Invalid status payload from server")
        }
    }

    suspend fun punch(
        serverUrl: String,
        token: String,
        action: String,
        location: String,
        clientTs: String = OffsetDateTime.now().toString(),
        eventId: String = UUID.randomUUID().toString()
    ): WatchStatus = withContext(Dispatchers.IO) {
        val base = serverUrl.trim().trimEnd('/')
        val payload = JSONObject()
            .put("action", action)
            .put("location", location)
            .put("client_ts", clientTs)
            .put("event_id", eventId)
            .toString()

        val req = Request.Builder()
            .url("$base/api/watch/punch")
            .header("Authorization", "Bearer $token")
            .post(payload.toRequestBody(jsonMediaType))
            .build()

        client.newCall(req).execute().use { resp ->
            val bodyStr = resp.body?.string() ?: ""
            if (resp.code == 401) {
                throw ApiAuthException("Token rejected (401)")
            }
            if (!resp.isSuccessful) {
                if (resp.code in 400..499) {
                    throw HttpPunchException(resp.code, "Punch rejected: HTTP ${resp.code}")
                }
                throw IOException("Punch failed: HTTP ${resp.code}")
            }
            WatchStatus.fromJsonString(bodyStr)
                ?: throw IOException("Invalid punch payload from server")
        }
    }

    suspend fun flushPendingPunches(store: TokenStore): WatchStatus? = flushMutex.withLock {
        // The token lives in EncryptedSharedPreferences (Keystore); reading it can be
        // slow, so never do it on the caller's (main) thread.
        val token = withContext(Dispatchers.IO) { store.token } ?: return@withLock null
        val pending = store.getPendingPunches()
        if (pending.isEmpty()) return@withLock null

        var latest: WatchStatus? = null
        for (p in pending) {
            try {
                val res = punch(
                    serverUrl = store.serverUrl,
                    token = token,
                    action = p.action,
                    location = p.location,
                    clientTs = p.clientTs,
                    eventId = p.eventId
                )
                store.removePendingPunch(p.eventId)
                store.saveCachedStatus(res)
                latest = res
            } catch (_: HttpPunchException) {
                // Permanent rejection: the server did not apply this event and the
                // event_id dedup makes a later retry safe, so drop it and continue.
                store.removePendingPunch(p.eventId)
            }
            // ApiAuthException / IOException (5xx, network): abort the loop and keep
            // the remaining punches queued, preserving tap order.
        }
        latest
    }

    /**
     * Persists the punch locally and returns the optimistic status immediately.
     * This only touches plain SharedPreferences — no Keystore token read, no network —
     * so callers can apply the result to the UI (and fire haptics) without blocking.
     * The queued [PunchSyncWorker] (and the caller's status refresh) flush it to the
     * server in the background; a permanent rejection is dropped by the flush and the
     * next status refresh reconciles the UI with the server.
     */
    suspend fun enqueuePunchLocally(
        context: Context,
        store: TokenStore,
        action: String,
        location: String
    ): WatchStatus = withContext(Dispatchers.IO) {
        val nowTs = OffsetDateTime.now().toString()
        val nowLocal = LocalDateTime.now()
        val localDate = nowLocal.toLocalDate().toString()
        val localHm = "%02d:%02d".format(nowLocal.hour, nowLocal.minute)
        val eventId = UUID.randomUUID().toString()

        store.enqueuePendingPunch(PendingPunch(action, location, nowTs, eventId))
        val optimistic = store.applyOptimisticPunch(action, location, localDate, localHm)
        // WorkManager init is not free; schedule it detached so it never delays
        // the optimistic UI response.
        queueScope.launch { PunchTileService.enqueueOfflineSync(context) }
        optimistic
    }
}
