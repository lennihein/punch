package com.lennihein.atoss.wear.tile

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import androidx.wear.protolayout.ActionBuilders
import androidx.wear.protolayout.ColorBuilders.argb
import androidx.wear.protolayout.DimensionBuilders.dp
import androidx.wear.protolayout.DimensionBuilders.expand
import androidx.wear.protolayout.DimensionBuilders.sp
import androidx.wear.protolayout.DimensionBuilders.wrap
import androidx.wear.protolayout.LayoutElementBuilders
import androidx.wear.protolayout.ModifiersBuilders
import androidx.wear.protolayout.ModifiersBuilders.Clickable
import androidx.wear.protolayout.ResourceBuilders
import androidx.wear.protolayout.TimelineBuilders
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders
import androidx.wear.tiles.TileService
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import com.lennihein.atoss.wear.data.PendingPunch
import com.lennihein.atoss.wear.data.TokenStore
import com.lennihein.atoss.wear.data.WatchStatus
import com.lennihein.atoss.wear.network.ApiAuthException
import com.lennihein.atoss.wear.network.AtossApiClient
import com.lennihein.atoss.wear.ui.MainActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import java.time.Duration
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.temporal.ChronoUnit
import java.util.UUID

private const val RESOURCES_VERSION = "7"

private const val CLICK_PUNCH_IN_REMOTE = "punch_in_remote"
private const val CLICK_PUNCH_IN_OFFICE = "punch_in_office"
private const val CLICK_PUNCH_OUT = "punch_out"
private const val CLICK_SWITCH_REMOTE = "switch_remote"
private const val CLICK_SWITCH_OFFICE = "switch_office"

class PunchTileService : TileService() {

    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onTileRequest(requestParams: RequestBuilders.TileRequest): ListenableFuture<TileBuilders.Tile> {
        val store = TokenStore(applicationContext)
        val token = store.token
        var cached = store.getCachedStatus()
        val clickedId = requestParams.currentState?.lastClickableId.orEmpty()

        var handledClick = false
        if (!token.isNullOrBlank() && clickedId.isNotEmpty()) {
            val punchSpec: Pair<String, String>? = when (clickedId) {
                CLICK_PUNCH_IN_REMOTE -> "in" to "remote"
                CLICK_PUNCH_IN_OFFICE -> "in" to "office"
                CLICK_PUNCH_OUT -> "out" to (cached?.activeEntry?.location ?: store.lastLocation)
                CLICK_SWITCH_REMOTE -> "switch_location" to "remote"
                CLICK_SWITCH_OFFICE -> "switch_location" to "office"
                else -> null
            }

            if (punchSpec != null) {
                handledClick = true
                val (action, location) = punchSpec
                val nowTs = OffsetDateTime.now().toString()
                val nowLocal = LocalDateTime.now()
                val localDate = nowLocal.toLocalDate().toString()
                val localHm = "%02d:%02d".format(nowLocal.hour, nowLocal.minute)
                val eventId = UUID.randomUUID().toString()

                store.enqueuePendingPunch(PendingPunch(action, location, nowTs, eventId))
                cached = store.applyOptimisticPunch(action, location, localDate, localHm)
                vibrateConfirm()

                enqueueOfflineSync(applicationContext)
                serviceScope.launch {
                    try {
                        AtossApiClient.flushPendingPunches(store)
                        val fresh = AtossApiClient.getStatus(store.serverUrl, token)
                        store.saveCachedStatus(fresh)
                        getUpdater(applicationContext).requestUpdate(PunchTileService::class.java)
                    } catch (_: ApiAuthException) {
                        // Ignore here; UI will prompt re-pairing when opened.
                    } catch (_: Exception) {
                        // Offline sync worker remains scheduled with NetworkType.CONNECTED.
                    }
                }
            }
        }

        val queuedCount = store.getPendingPunches().size
        if (!token.isNullOrBlank() && !handledClick) {
            val ageMs = System.currentTimeMillis() - store.getCachedStatusTimestampMs()
            if (queuedCount > 0) {
                enqueueOfflineSync(applicationContext)
            } else if (ageMs > 60_000L) {
                enqueueBackgroundRefresh(applicationContext)
            }
        }

        val timelineBuilder = TimelineBuilders.Timeline.Builder()
        val nowLocal = LocalDateTime.now()
        val activeEntry = cached?.activeEntry
        if (!token.isNullOrBlank() && cached?.clockedIn == true && activeEntry != null) {
            val startDt = try {
                LocalDateTime.of(LocalDate.parse(activeEntry.date), LocalTime.parse(activeEntry.start))
            } catch (_: Exception) {
                null
            }
            if (startDt != null) {
                val zone = ZoneId.systemDefault()
                val baseMinute = nowLocal.truncatedTo(ChronoUnit.MINUTES)
                for (offsetMin in 0 until 30) {
                    val slotStart = baseMinute.plusMinutes(offsetMin.toLong())
                    val slotEnd = slotStart.plusMinutes(1)
                    val elapsedMins = Duration.between(startDt, slotStart).toMinutes().toInt().coerceAtLeast(0)
                    val startMs = slotStart.atZone(zone).toInstant().toEpochMilli()
                    val endMs = slotEnd.atZone(zone).toInstant().toEpochMilli()
                    val slotLayout = buildTileLayout(token, cached, queuedCount, elapsedMins)
                    timelineBuilder.addTimelineEntry(
                        TimelineBuilders.TimelineEntry.Builder()
                            .setValidity(
                                TimelineBuilders.TimeInterval.Builder()
                                    .setStartMillis(startMs)
                                    .setEndMillis(endMs)
                                    .build()
                            )
                            .setLayout(
                                LayoutElementBuilders.Layout.Builder()
                                    .setRoot(slotLayout)
                                    .build()
                            )
                            .build()
                    )
                }
                val fallbackElapsed = Duration.between(startDt, nowLocal).toMinutes().toInt().coerceAtLeast(0)
                val fallbackLayout = buildTileLayout(token, cached, queuedCount, fallbackElapsed)
                timelineBuilder.addTimelineEntry(
                    TimelineBuilders.TimelineEntry.Builder()
                        .setLayout(
                            LayoutElementBuilders.Layout.Builder()
                                .setRoot(fallbackLayout)
                                .build()
                        )
                        .build()
                )
            } else {
                val layout = buildTileLayout(token, cached, queuedCount, 0)
                timelineBuilder.addTimelineEntry(
                    TimelineBuilders.TimelineEntry.Builder()
                        .setLayout(
                            LayoutElementBuilders.Layout.Builder()
                                .setRoot(layout)
                                .build()
                        )
                        .build()
                )
            }
        } else {
            val layout = buildTileLayout(token, cached, queuedCount, 0)
            timelineBuilder.addTimelineEntry(
                TimelineBuilders.TimelineEntry.Builder()
                    .setLayout(
                        LayoutElementBuilders.Layout.Builder()
                            .setRoot(layout)
                            .build()
                    )
                    .build()
            )
        }

        val tile = TileBuilders.Tile.Builder()
            .setResourcesVersion(RESOURCES_VERSION)
            .setFreshnessIntervalMillis(15 * 60 * 1000L)
            .setTileTimeline(timelineBuilder.build())
            .build()

        return Futures.immediateFuture(tile)
    }

    override fun onTileResourcesRequest(
        requestParams: RequestBuilders.ResourcesRequest
    ): ListenableFuture<ResourceBuilders.Resources> {
        return Futures.immediateFuture(
            ResourceBuilders.Resources.Builder()
                .setVersion(RESOURCES_VERSION)
                .build()
        )
    }

    private fun buildTileLayout(
        token: String?,
        cached: WatchStatus?,
        queuedCount: Int,
        activeElapsedMins: Int = 0
    ): LayoutElementBuilders.LayoutElement {
        val openMainClickable = Clickable.Builder()
            .setId("open_app")
            .setOnClick(
                ActionBuilders.LaunchAction.Builder()
                    .setAndroidActivity(
                        ActionBuilders.AndroidActivity.Builder()
                            .setPackageName(packageName)
                            .setClassName(MainActivity::class.java.name)
                            .build()
                    )
                    .build()
            )
            .build()

        if (token.isNullOrBlank()) {
            return LayoutElementBuilders.Box.Builder()
                .setWidth(expand())
                .setHeight(expand())
                .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
                .setVerticalAlignment(LayoutElementBuilders.VERTICAL_ALIGN_CENTER)
                .setModifiers(
                    ModifiersBuilders.Modifiers.Builder()
                        .setBackground(
                            ModifiersBuilders.Background.Builder()
                                .setColor(argb(0xFF0B0F17.toInt()))
                                .build()
                        )
                        .setPadding(
                            ModifiersBuilders.Padding.Builder()
                                .setAll(dp(16f))
                                .build()
                        )
                        .build()
                )
                .addContent(
                    LayoutElementBuilders.Column.Builder()
                        .setWidth(expand())
                        .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
                        .addContent(
                            styledText("PUNCH!", 11f, 0xFF94A3B8.toInt(), bold = true)
                        )
                        .addContent(
                            LayoutElementBuilders.Spacer.Builder().setHeight(dp(10f)).build()
                        )
                        .addContent(
                            styledText("Not Paired", 20f, 0xFFF8FAFC.toInt(), bold = true)
                        )
                        .addContent(
                            LayoutElementBuilders.Spacer.Builder().setHeight(dp(14f)).build()
                        )
                        .addContent(
                            pillButton("Pair Watch", 0xFF4F46E5.toInt(), 0xFFFFFFFF.toInt(), openMainClickable)
                        )
                        .build()
                )
                .build()
        }

        val isClockedIn = cached?.clockedIn == true
        val activeEntry = cached?.activeEntry
        val currentLoc = activeEntry?.location ?: cached?.lastLocation ?: "remote"
        val syncSuffix = if (queuedCount > 0) " · ${queuedCount}q" else ""

        val statusText = if (isClockedIn) {
            "● ${currentLoc.uppercase()}$syncSuffix"
        } else {
            "○ OFF DUTY$syncSuffix"
        }
        val statusColor = if (isClockedIn) 0xFF34D399.toInt() else 0xFF94A3B8.toInt()

        val completedMins = cached?.todayCompletedMinutes ?: 0
        val breakMins = cached?.breakActualMinutes ?: 0
        val todayStr = LocalDate.now().toString()
        val elapsedTodayMins = if (isClockedIn && activeEntry?.date == todayStr) activeElapsedMins else 0
        val totalTodayMins = completedMins + elapsedTodayMins
        val shownDurationMins = if (isClockedIn) activeElapsedMins else 0

        val heroTitle = "%02d:%02d".format(shownDurationMins / 60, shownDurationMins % 60)
        val heroSubtitle = "Today %02d:%02d · Break %dm".format(
            totalTodayMins / 60,
            totalTodayMins % 60,
            breakMins
        )

        val actionRow = LayoutElementBuilders.Row.Builder()
            .setWidth(wrap())
            .setVerticalAlignment(LayoutElementBuilders.VERTICAL_ALIGN_CENTER)

        if (isClockedIn) {
            actionRow.addContent(
                pillButton(
                    label = "Clock Out",
                    bgColor = 0xFFE11D48.toInt(),
                    textColor = 0xFFFFFFFF.toInt(),
                    clickable = loadActionClickable(CLICK_PUNCH_OUT)
                )
            )
            actionRow.addContent(
                LayoutElementBuilders.Spacer.Builder().setWidth(dp(6f)).build()
            )
            val switchToOffice = currentLoc != "office"
            actionRow.addContent(
                pillButton(
                    label = if (switchToOffice) "→ Office" else "→ Remote",
                    bgColor = 0xFF334155.toInt(),
                    textColor = 0xFFF8FAFC.toInt(),
                    clickable = loadActionClickable(
                        if (switchToOffice) CLICK_SWITCH_OFFICE else CLICK_SWITCH_REMOTE
                    )
                )
            )
        } else {
            actionRow.addContent(
                pillButton(
                    label = "Remote",
                    bgColor = 0xFF0D9488.toInt(),
                    textColor = 0xFFFFFFFF.toInt(),
                    clickable = loadActionClickable(CLICK_PUNCH_IN_REMOTE)
                )
            )
            actionRow.addContent(
                LayoutElementBuilders.Spacer.Builder().setWidth(dp(6f)).build()
            )
            actionRow.addContent(
                pillButton(
                    label = "Office",
                    bgColor = 0xFF4F46E5.toInt(),
                    textColor = 0xFFFFFFFF.toInt(),
                    clickable = loadActionClickable(CLICK_PUNCH_IN_OFFICE)
                )
            )
        }

        val hasWarnings = cached?.warnings.orEmpty().isNotEmpty()

        val statusPill = LayoutElementBuilders.Box.Builder()
            .setWidth(wrap())
            .setHeight(wrap())
            .setModifiers(
                ModifiersBuilders.Modifiers.Builder()
                    .setBackground(
                        ModifiersBuilders.Background.Builder()
                            .setColor(argb(0xFF1E293B.toInt()))
                            .setCorner(
                                ModifiersBuilders.Corner.Builder()
                                    .setRadius(dp(999f))
                                    .build()
                            )
                            .build()
                    )
                    .setPadding(
                        ModifiersBuilders.Padding.Builder()
                            .setStart(dp(10f))
                            .setEnd(dp(10f))
                            .setTop(dp(4f))
                            .setBottom(dp(4f))
                            .build()
                    )
                    .setClickable(openMainClickable)
                    .build()
            )
            .addContent(styledText(statusText, 11f, statusColor, bold = true))
            .build()

        val statusRowBuilder = LayoutElementBuilders.Row.Builder()
            .setWidth(wrap())
            .setVerticalAlignment(LayoutElementBuilders.VERTICAL_ALIGN_CENTER)
            .addContent(statusPill)

        if (hasWarnings) {
            statusRowBuilder.addContent(
                LayoutElementBuilders.Spacer.Builder().setWidth(dp(5f)).build()
            )
            statusRowBuilder.addContent(
                LayoutElementBuilders.Box.Builder()
                    .setWidth(dp(9f))
                    .setHeight(dp(9f))
                    .setModifiers(
                        ModifiersBuilders.Modifiers.Builder()
                            .setBackground(
                                ModifiersBuilders.Background.Builder()
                                    .setColor(argb(0xFFF59E0B.toInt()))
                                    .setCorner(
                                        ModifiersBuilders.Corner.Builder()
                                            .setRadius(dp(5f))
                                            .build()
                                    )
                                    .build()
                            )
                            .setClickable(openMainClickable)
                            .build()
                    )
                    .build()
            )
        }
        val statusHeader = statusRowBuilder.build()

        val heroBlock = LayoutElementBuilders.Column.Builder()
            .setWidth(wrap())
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
            .setModifiers(
                ModifiersBuilders.Modifiers.Builder()
                    .setClickable(openMainClickable)
                    .setPadding(
                        ModifiersBuilders.Padding.Builder()
                            .setTop(dp(4f))
                            .setBottom(dp(4f))
                            .setStart(dp(8f))
                            .setEnd(dp(8f))
                            .build()
                    )
                    .build()
            )
            .addContent(styledText(heroTitle, 24f, 0xFFF8FAFC.toInt(), bold = true))
            .addContent(LayoutElementBuilders.Spacer.Builder().setHeight(dp(2f)).build())
            .addContent(styledText(heroSubtitle, 11f, 0xFF94A3B8.toInt(), bold = false))
            .build()

        return LayoutElementBuilders.Box.Builder()
            .setWidth(expand())
            .setHeight(expand())
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
            .setVerticalAlignment(LayoutElementBuilders.VERTICAL_ALIGN_CENTER)
            .setModifiers(
                ModifiersBuilders.Modifiers.Builder()
                    .setBackground(
                        ModifiersBuilders.Background.Builder()
                            .setColor(argb(0xFF0B0F17.toInt()))
                            .build()
                    )
                    .setPadding(
                        ModifiersBuilders.Padding.Builder()
                            .setStart(dp(10f))
                            .setEnd(dp(10f))
                            .setTop(dp(14f))
                            .setBottom(dp(14f))
                            .build()
                    )
                    .build()
            )
            .addContent(
                LayoutElementBuilders.Column.Builder()
                    .setWidth(expand())
                    .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
                    .addContent(statusHeader)
                    .addContent(LayoutElementBuilders.Spacer.Builder().setHeight(dp(6f)).build())
                    .addContent(heroBlock)
                    .addContent(LayoutElementBuilders.Spacer.Builder().setHeight(dp(10f)).build())
                    .addContent(actionRow.build())
                    .build()
            )
            .build()
    }

    private fun styledText(
        value: String,
        sizeSp: Float,
        colorArgb: Int,
        bold: Boolean
    ): LayoutElementBuilders.Text {
        val fontStyle = LayoutElementBuilders.FontStyle.Builder()
            .setSize(sp(sizeSp))
            .setColor(argb(colorArgb))
            .setWeight(
                if (bold) LayoutElementBuilders.FONT_WEIGHT_BOLD
                else LayoutElementBuilders.FONT_WEIGHT_NORMAL
            )
            .build()
        return LayoutElementBuilders.Text.Builder()
            .setText(value)
            .setMaxLines(1)
            .setFontStyle(fontStyle)
            .build()
    }

    private fun pillButton(
        label: String,
        bgColor: Int,
        textColor: Int,
        clickable: Clickable
    ): LayoutElementBuilders.LayoutElement {
        return LayoutElementBuilders.Box.Builder()
            .setWidth(wrap())
            .setHeight(dp(38f))
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
            .setVerticalAlignment(LayoutElementBuilders.VERTICAL_ALIGN_CENTER)
            .setModifiers(
                ModifiersBuilders.Modifiers.Builder()
                    .setBackground(
                        ModifiersBuilders.Background.Builder()
                            .setColor(argb(bgColor))
                            .setCorner(
                                ModifiersBuilders.Corner.Builder()
                                    .setRadius(dp(19f))
                                    .build()
                            )
                            .build()
                    )
                    .setPadding(
                        ModifiersBuilders.Padding.Builder()
                            .setStart(dp(14f))
                            .setEnd(dp(14f))
                            .build()
                    )
                    .setClickable(clickable)
                    .build()
            )
            .addContent(styledText(label, 12.5f, textColor, bold = true))
            .build()
    }

    private fun loadActionClickable(id: String): Clickable {
        return Clickable.Builder()
            .setId(id)
            .setOnClick(ActionBuilders.LoadAction.Builder().build())
            .build()
    }

    @Suppress("DEPRECATION")
    private fun vibrateConfirm() {
        val vibrator = getSystemService(Vibrator::class.java) ?: return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            vibrator.vibrate(VibrationEffect.createOneShot(40L, VibrationEffect.DEFAULT_AMPLITUDE))
        } else {
            vibrator.vibrate(40L)
        }
    }

    companion object {
        fun enqueueBackgroundRefresh(context: Context) {
            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()
            val req = OneTimeWorkRequestBuilder<StatusRefreshWorker>()
                .setConstraints(constraints)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(
                "atoss_tile_status_refresh",
                ExistingWorkPolicy.KEEP,
                req
            )
        }

        fun enqueueOfflineSync(context: Context) {
            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()
            val req = OneTimeWorkRequestBuilder<PunchSyncWorker>()
                .setConstraints(constraints)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(
                "atoss_offline_punch_sync",
                ExistingWorkPolicy.REPLACE,
                req
            )
        }
    }
}

class StatusRefreshWorker(
    appContext: Context,
    params: WorkerParameters
) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val store = TokenStore(applicationContext)
        val token = store.token ?: return Result.success()
        return try {
            AtossApiClient.flushPendingPunches(store)
            val fresh = AtossApiClient.getStatus(store.serverUrl, token)
            store.saveCachedStatus(fresh)
            TileService.getUpdater(applicationContext).requestUpdate(PunchTileService::class.java)
            Result.success()
        } catch (_: ApiAuthException) {
            Result.failure()
        } catch (_: Exception) {
            Result.retry()
        }
    }
}

class PunchSyncWorker(
    appContext: Context,
    params: WorkerParameters
) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val store = TokenStore(applicationContext)
        val token = store.token ?: return Result.success()
        return try {
            AtossApiClient.flushPendingPunches(store)
            val fresh = AtossApiClient.getStatus(store.serverUrl, token)
            store.saveCachedStatus(fresh)
            TileService.getUpdater(applicationContext).requestUpdate(PunchTileService::class.java)
            Result.success()
        } catch (_: ApiAuthException) {
            Result.failure()
        } catch (_: Exception) {
            Result.retry()
        }
    }
}

class TileRefreshReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val action = intent?.action ?: return
        if (action == Intent.ACTION_MY_PACKAGE_REPLACED || action == ACTION_REFRESH_TILE) {
            val appCtx = context.applicationContext
            TileService.getUpdater(appCtx).requestUpdate(PunchTileService::class.java)
            if (!TokenStore(appCtx).token.isNullOrBlank()) {
                PunchTileService.enqueueBackgroundRefresh(appCtx)
            }
        }
    }

    companion object {
        const val ACTION_REFRESH_TILE = "com.lennihein.atoss.wear.REFRESH_TILE"
    }
}
