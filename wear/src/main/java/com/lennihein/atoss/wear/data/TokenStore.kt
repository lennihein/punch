package com.lennihein.atoss.wear.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import org.json.JSONArray
import org.json.JSONObject
import java.time.Duration
import java.time.LocalDate
import java.time.LocalTime

data class ActiveEntry(
    val id: Int,
    val start: String,
    val location: String,
    val date: String
)

data class PendingPunch(
    val action: String,
    val location: String,
    val clientTs: String,
    val eventId: String
)

data class WatchStatus(
    val clockedIn: Boolean,
    val activeEntry: ActiveEntry?,
    val todayCompletedMinutes: Int,
    val breakRequiredMinutes: Int,
    val breakActualMinutes: Int,
    val warnings: List<String>,
    val lastLocation: String
) {
    fun toJsonString(): String {
        val root = JSONObject()
        root.put("clocked_in", clockedIn)
        if (activeEntry != null) {
            val ae = JSONObject()
            ae.put("id", activeEntry.id)
            ae.put("start", activeEntry.start)
            ae.put("location", activeEntry.location)
            ae.put("date", activeEntry.date)
            root.put("active_entry", ae)
        } else {
            root.put("active_entry", JSONObject.NULL)
        }
        root.put("today_completed_minutes", todayCompletedMinutes)
        root.put("break_required_minutes", breakRequiredMinutes)
        root.put("break_actual_minutes", breakActualMinutes)
        root.put("warnings", JSONArray(warnings))
        root.put("last_location", lastLocation)
        return root.toString()
    }

    companion object {
        fun fromJsonString(raw: String): WatchStatus? {
            return try {
                val obj = JSONObject(raw)
                val aeObj = obj.optJSONObject("active_entry")
                val active = if (aeObj != null) {
                    ActiveEntry(
                        id = aeObj.optInt("id", 0),
                        start = aeObj.optString("start", "00:00"),
                        location = aeObj.optString("location", "remote"),
                        date = aeObj.optString("date", "")
                    )
                } else {
                    null
                }
                val warnArr = obj.optJSONArray("warnings") ?: JSONArray()
                val warnings = buildList {
                    for (i in 0 until warnArr.length()) {
                        add(warnArr.optString(i))
                    }
                }
                WatchStatus(
                    clockedIn = obj.optBoolean("clocked_in", false),
                    activeEntry = active,
                    todayCompletedMinutes = obj.optInt("today_completed_minutes", 0),
                    breakRequiredMinutes = obj.optInt("break_required_minutes", 0),
                    breakActualMinutes = obj.optInt("break_actual_minutes", 0),
                    warnings = warnings,
                    lastLocation = obj.optString("last_location", "remote")
                )
            } catch (_: Exception) {
                null
            }
        }
    }
}

class TokenStore(context: Context) {

    private val prefs: SharedPreferences = run {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            "atoss_wear_secure_prefs",
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
        )
    }

    var serverUrl: String
        get() = prefs.getString(KEY_SERVER_URL, DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL
        set(value) {
            prefs.edit().putString(KEY_SERVER_URL, value.trim().trimEnd('/')).apply()
        }

    var token: String?
        get() = prefs.getString(KEY_TOKEN, null)
        set(value) {
            prefs.edit().putString(KEY_TOKEN, value).apply()
        }

    var userName: String?
        get() = prefs.getString(KEY_USER_NAME, null)
        set(value) {
            prefs.edit().putString(KEY_USER_NAME, value).apply()
        }

    var lastLocation: String
        get() = prefs.getString(KEY_LAST_LOCATION, "remote") ?: "remote"
        set(value) {
            prefs.edit().putString(KEY_LAST_LOCATION, value).apply()
        }

    fun saveCachedStatus(status: WatchStatus) {
        lastLocation = status.lastLocation
        prefs.edit()
            .putString(KEY_CACHED_STATUS, status.toJsonString())
            .putLong(KEY_CACHED_TS, System.currentTimeMillis())
            .apply()
    }

    fun getCachedStatus(): WatchStatus? {
        val raw = prefs.getString(KEY_CACHED_STATUS, null) ?: return null
        return WatchStatus.fromJsonString(raw)
    }

    fun getCachedStatusTimestampMs(): Long {
        return prefs.getLong(KEY_CACHED_TS, 0L)
    }

    @Synchronized
    fun getPendingPunches(): List<PendingPunch> {
        val raw = prefs.getString(KEY_PENDING_PUNCHES, null) ?: return emptyList()
        return try {
            val arr = JSONArray(raw)
            buildList {
                for (i in 0 until arr.length()) {
                    val obj = arr.optJSONObject(i) ?: continue
                    add(
                        PendingPunch(
                            action = obj.optString("action", "toggle"),
                            location = obj.optString("location", "remote"),
                            clientTs = obj.optString("client_ts", ""),
                            eventId = obj.optString("event_id", "")
                        )
                    )
                }
            }
        } catch (_: Exception) {
            emptyList()
        }
    }

    @Synchronized
    fun enqueuePendingPunch(punch: PendingPunch) {
        val current = getPendingPunches().toMutableList()
        if (current.none { it.eventId == punch.eventId }) {
            current.add(punch)
            savePendingPunches(current)
        }
    }

    @Synchronized
    fun removePendingPunch(eventId: String) {
        val remaining = getPendingPunches().filterNot { it.eventId == eventId }
        savePendingPunches(remaining)
    }

    private fun savePendingPunches(list: List<PendingPunch>) {
        val arr = JSONArray()
        for (p in list) {
            arr.put(
                JSONObject()
                    .put("action", p.action)
                    .put("location", p.location)
                    .put("client_ts", p.clientTs)
                    .put("event_id", p.eventId)
            )
        }
        prefs.edit().putString(KEY_PENDING_PUNCHES, arr.toString()).apply()
    }

    fun applyOptimisticPunch(
        action: String,
        location: String,
        localDate: String,
        localHm: String
    ): WatchStatus {
        val base = getCachedStatus() ?: WatchStatus(
            clockedIn = false,
            activeEntry = null,
            todayCompletedMinutes = 0,
            breakRequiredMinutes = 0,
            breakActualMinutes = 0,
            warnings = emptyList(),
            lastLocation = lastLocation
        )
        val effectiveAction = when (action.lowercase()) {
            "toggle" -> if (base.clockedIn) "out" else "in"
            else -> action.lowercase()
        }
        val cleanLoc = location.ifBlank { base.lastLocation }

        fun elapsedSinceActive(active: ActiveEntry?): Int {
            if (active == null || active.date != localDate) return 0
            return try {
                val startT = LocalTime.parse(active.start)
                val endT = LocalTime.parse(localHm)
                Duration.between(startT, endT).toMinutes().toInt().coerceAtLeast(0)
            } catch (_: Exception) {
                0
            }
        }

        val updated = when (effectiveAction) {
            "in" -> {
                if (base.clockedIn && base.activeEntry != null) {
                    if (base.activeEntry.location != cleanLoc && base.activeEntry.date == localDate) {
                        val added = elapsedSinceActive(base.activeEntry)
                        base.copy(
                            clockedIn = true,
                            activeEntry = ActiveEntry(-1, localHm, cleanLoc, localDate),
                            todayCompletedMinutes = base.todayCompletedMinutes + added,
                            lastLocation = cleanLoc
                        )
                    } else {
                        base
                    }
                } else {
                    base.copy(
                        clockedIn = true,
                        activeEntry = ActiveEntry(-1, localHm, cleanLoc, localDate),
                        lastLocation = cleanLoc
                    )
                }
            }
            "switch_location" -> {
                if (!base.clockedIn || base.activeEntry == null) {
                    base.copy(
                        clockedIn = true,
                        activeEntry = ActiveEntry(-1, localHm, cleanLoc, localDate),
                        lastLocation = cleanLoc
                    )
                } else {
                    val added = elapsedSinceActive(base.activeEntry)
                    base.copy(
                        clockedIn = true,
                        activeEntry = ActiveEntry(-1, localHm, cleanLoc, localDate),
                        todayCompletedMinutes = base.todayCompletedMinutes + added,
                        lastLocation = cleanLoc
                    )
                }
            }
            "out" -> {
                val added = if (base.clockedIn) elapsedSinceActive(base.activeEntry) else 0
                base.copy(
                    clockedIn = false,
                    activeEntry = null,
                    todayCompletedMinutes = base.todayCompletedMinutes + added,
                    lastLocation = base.activeEntry?.location ?: base.lastLocation
                )
            }
            else -> base
        }

        saveCachedStatus(updated)
        return updated
    }

    fun clearAuth() {
        prefs.edit()
            .remove(KEY_TOKEN)
            .remove(KEY_USER_NAME)
            .remove(KEY_CACHED_STATUS)
            .remove(KEY_CACHED_TS)
            .remove(KEY_PENDING_PUNCHES)
            .apply()
    }

    companion object {
        const val DEFAULT_SERVER_URL = "https://punch.example.com"
        private const val KEY_SERVER_URL = "server_url"
        private const val KEY_TOKEN = "watch_token"
        private const val KEY_USER_NAME = "user_name"
        private const val KEY_LAST_LOCATION = "last_location"
        private const val KEY_CACHED_STATUS = "cached_status_json"
        private const val KEY_CACHED_TS = "cached_status_ts"
        private const val KEY_PENDING_PUNCHES = "pending_punches_json"
    }
}
