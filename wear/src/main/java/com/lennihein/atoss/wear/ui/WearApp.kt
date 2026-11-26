package com.lennihein.atoss.wear.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.foundation.lazy.ScalingLazyColumn
import androidx.wear.compose.foundation.lazy.rememberScalingLazyListState
import androidx.wear.compose.material.Button
import androidx.wear.compose.material.ButtonDefaults
import androidx.wear.compose.material.Chip
import androidx.wear.compose.material.ChipDefaults
import androidx.wear.compose.material.CircularProgressIndicator
import androidx.wear.compose.material.Colors
import androidx.wear.compose.material.CompactButton
import androidx.wear.compose.material.CompactChip
import androidx.wear.compose.material.Icon
import androidx.wear.compose.material.MaterialTheme
import androidx.wear.compose.material.PositionIndicator
import androidx.wear.compose.material.Scaffold
import androidx.wear.compose.material.Text
import androidx.wear.compose.material.TimeText
import androidx.wear.tiles.TileService
import com.lennihein.atoss.wear.R
import com.lennihein.atoss.wear.data.TokenStore
import com.lennihein.atoss.wear.data.WatchStatus
import com.lennihein.atoss.wear.network.ApiAuthException
import com.lennihein.atoss.wear.network.AtossApiClient
import com.lennihein.atoss.wear.tile.PunchTileService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.Duration
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.OffsetDateTime

private val SlateBg = Color(0xFF0B0F17)
private val SlateSurface = Color(0xFF1E293B)
private val SlateBorder = Color(0xFF334155)
private val SlateMuted = Color(0xFF94A3B8)
private val SlateText = Color(0xFFF8FAFC)
private val TealPrimary = Color(0xFF0D9488)
private val TealAccent = Color(0xFF14B8A6)
private val EmeraldBright = Color(0xFF34D399)
private val IndigoAccent = Color(0xFF4F46E5)
private val RoseAction = Color(0xFFE11D48)

private const val QUEUE_STUCK_MINUTES = 5L
private val AmberWarn = Color(0xFF92400E)

private val WatchColors = Colors(
    primary = TealAccent,
    primaryVariant = TealPrimary,
    secondary = Color(0xFF6366F1),
    secondaryVariant = IndigoAccent,
    background = SlateBg,
    surface = SlateSurface,
    error = Color(0xFFF43F5E),
    onPrimary = Color(0xFF042F2E),
    onSecondary = Color.White,
    onBackground = SlateText,
    onSurface = SlateText,
    onSurfaceVariant = SlateMuted,
    onError = Color.White
)

@Composable
fun WearApp(store: TokenStore) {
    MaterialTheme(colors = WatchColors) {
        var token by remember { mutableStateOf<String?>(null) }
        var loaded by remember { mutableStateOf(false) }
        LaunchedEffect(Unit) {
            token = withContext(Dispatchers.IO) { store.token }
            loaded = true
        }
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(SlateBg)
        ) {
            if (!loaded) {
                // Encrypted store still loading; keep the splash colour.
            } else if (token.isNullOrBlank()) {
                PairingScreen(
                    store = store,
                    onPaired = { newToken -> token = newToken }
                )
            } else {
                PunchScreen(
                    store = store,
                    token = token!!,
                    onUnpaired = {
                        store.clearAuth()
                        token = null
                    }
                )
            }
        }
    }
}

@Composable
private fun PairingScreen(
    store: TokenStore,
    onPaired: (String) -> Unit
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val haptic = LocalHapticFeedback.current
    val listState = rememberScalingLazyListState()
    var code by remember { mutableStateOf("") }
    val serverUrl by remember { mutableStateOf(store.serverUrl) }
    var showServerSelector by remember { mutableStateOf(false) }
    var loading by remember { mutableStateOf(false) }
    var errorMsg by remember { mutableStateOf<String?>(null) }

    Scaffold(
        timeText = { TimeText() },
        positionIndicator = { PositionIndicator(scalingLazyListState = listState) }
    ) {
        ScalingLazyColumn(
            state = listState,
            modifier = Modifier
                .fillMaxSize()
                .background(SlateBg),
            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 24.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            item {
                Text(
                    text = "Pair Watch",
                    fontSize = 14.sp,
                    fontWeight = FontWeight.SemiBold,
                    color = SlateText,
                    textAlign = TextAlign.Center
                )
            }
            item {
                Box(
                    modifier = Modifier
                        .clip(RoundedCornerShape(12.dp))
                        .background(SlateSurface)
                        .border(1.dp, SlateBorder, RoundedCornerShape(12.dp))
                        .padding(horizontal = 14.dp, vertical = 6.dp)
                ) {
                    Text(
                        text = if (code.isEmpty()) "_ _ _  _ _ _" else code.chunked(3).joinToString("  "),
                        fontSize = 18.sp,
                        fontWeight = FontWeight.Bold,
                        color = EmeraldBright,
                        style = TextStyle(fontFeatureSettings = "tnum")
                    )
                }
            }
            if (errorMsg != null) {
                item {
                    Text(
                        text = errorMsg!!,
                        color = Color(0xFFFCA5A5),
                        fontSize = 11.sp,
                        textAlign = TextAlign.Center,
                        modifier = Modifier.padding(horizontal = 12.dp)
                    )
                }
            }
            item {
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(5.dp)
                ) {
                    listOf(
                        listOf("1", "2", "3"),
                        listOf("4", "5", "6"),
                        listOf("7", "8", "9"),
                        listOf("C", "0", "⌫")
                    ).forEach { row ->
                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            row.forEach { key ->
                                CompactButton(
                                    onClick = {
                                        haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                        errorMsg = null
                                        when (key) {
                                            "C" -> code = ""
                                            "⌫" -> if (code.isNotEmpty()) code = code.dropLast(1)
                                            else -> if (code.length < 6) code += key
                                        }
                                    },
                                    colors = ButtonDefaults.buttonColors(
                                        backgroundColor = SlateSurface,
                                        contentColor = SlateText
                                    ),
                                    modifier = Modifier.size(38.dp)
                                ) {
                                    Text(key, fontSize = 14.sp, fontWeight = FontWeight.Medium)
                                }
                            }
                        }
                    }
                }
            }
            item {
                Button(
                    onClick = {
                        if (code.length != 6 || loading) return@Button
                        loading = true
                        errorMsg = null
                        scope.launch {
                            try {
                                store.serverUrl = serverUrl
                                val res = AtossApiClient.pair(serverUrl, code)
                                store.token = res.token
                                store.userName = res.displayName
                                haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                                TileService.getUpdater(context).requestUpdate(PunchTileService::class.java)
                                onPaired(res.token)
                            } catch (e: Exception) {
                                errorMsg = e.message ?: "Pairing error"
                            } finally {
                                loading = false
                            }
                        }
                    },
                    enabled = code.length == 6 && !loading,
                    colors = ButtonDefaults.buttonColors(
                        backgroundColor = IndigoAccent,
                        contentColor = Color.White,
                        disabledBackgroundColor = SlateSurface,
                        disabledContentColor = SlateMuted
                    ),
                    modifier = Modifier
                        .fillMaxWidth(0.84f)
                        .height(42.dp)
                ) {
                    if (loading) {
                        CircularProgressIndicator(modifier = Modifier.size(18.dp))
                    } else {
                        Text("Connect", fontWeight = FontWeight.SemiBold)
                    }
                }
            }
            item {
                CompactChip(
                    onClick = { showServerSelector = !showServerSelector },
                    label = { Text(if (showServerSelector) "Hide URL" else "Server URL", fontSize = 11.sp) },
                    colors = ChipDefaults.chipColors(backgroundColor = SlateSurface, contentColor = SlateMuted)
                )
            }
            if (showServerSelector) {
                item {
                    Text(
                        text = serverUrl,
                        fontSize = 10.sp,
                        color = SlateMuted,
                        textAlign = TextAlign.Center,
                        modifier = Modifier.padding(horizontal = 12.dp)
                    )
                }
            }
        }
    }
}

@Composable
private fun PunchScreen(
    store: TokenStore,
    token: String,
    onUnpaired: () -> Unit
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val haptic = LocalHapticFeedback.current
    val listState = rememberScalingLazyListState(initialCenterItemIndex = 0)

    var status by remember { mutableStateOf<WatchStatus?>(null) }
    var pendingCount by remember { mutableIntStateOf(0) }
    var queueStuck by remember { mutableStateOf(false) }
    var busyAction by remember { mutableStateOf<String?>(null) }
    var errorMsg by remember { mutableStateOf<String?>(null) }
    var activeElapsedMin by remember { mutableIntStateOf(0) }

    suspend fun readQueueState() {
        val (count, stuck) = withContext(Dispatchers.IO) {
            val pending = store.getPendingPunches()
            val stuck = pending.any { p ->
                try {
                    Duration.between(OffsetDateTime.parse(p.clientTs), OffsetDateTime.now()).toMinutes() >= QUEUE_STUCK_MINUTES
                } catch (_: Exception) {
                    false
                }
            }
            pending.size to stuck
        }
        pendingCount = count
        queueStuck = stuck
    }

    suspend fun refreshStatus() {
        try {
            AtossApiClient.flushPendingPunches(store)
            val fresh = AtossApiClient.getStatus(store.serverUrl, token)
            status = fresh
            store.saveCachedStatus(fresh)
            readQueueState()
            errorMsg = null
            TileService.getUpdater(context).requestUpdate(PunchTileService::class.java)
        } catch (_: ApiAuthException) {
            onUnpaired()
        } catch (e: Exception) {
            readQueueState()
            errorMsg = if (pendingCount > 0) "Offline ($pendingCount queued)" else (e.message ?: "Offline")
        }
    }

    fun triggerPunch(action: String, location: String) {
        if (busyAction != null) return
        busyAction = "$action:$location"
        errorMsg = null
        scope.launch {
            try {
                val updated = AtossApiClient.performOrQueuePunch(
                    context = context,
                    store = store,
                    action = action,
                    location = location
                )
                status = updated
                readQueueState()
                haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                TileService.getUpdater(context).requestUpdate(PunchTileService::class.java)
            } catch (_: ApiAuthException) {
                onUnpaired()
            } catch (e: Exception) {
                errorMsg = e.message ?: "Punch failed"
            } finally {
                busyAction = null
            }
            // Reconcile with the server in the background; the punch is already visible.
            refreshStatus()
        }
    }

    LaunchedEffect(token) {
        status = withContext(Dispatchers.IO) { store.getCachedStatus() }
        refreshStatus()
    }

    LaunchedEffect(status?.clockedIn, status?.activeEntry?.start, status?.activeEntry?.date) {
        while (true) {
            val ae = status?.activeEntry
            activeElapsedMin = if (status?.clockedIn == true && ae != null) {
                computeElapsedMinutes(ae.date, ae.start)
            } else {
                0
            }
            delay(5_000L)
        }
    }

    val isClockedIn = status?.clockedIn == true
    val activeEntry = status?.activeEntry
    val activeLoc = activeEntry?.location ?: status?.lastLocation ?: store.lastLocation
    val completedMin = status?.todayCompletedMinutes ?: 0
    val breakActual = status?.breakActualMinutes ?: 0
    val todayStr = LocalDate.now().toString()
    val elapsedTodayMin = if (isClockedIn && activeEntry?.date == todayStr) activeElapsedMin else 0
    val totalTodayMin = completedMin + elapsedTodayMin
    val shownDurationMin = if (isClockedIn) activeElapsedMin else 0
    val warnings = status?.warnings.orEmpty()

    Scaffold(
        timeText = { TimeText() },
        positionIndicator = { PositionIndicator(scalingLazyListState = listState) }
    ) {
        ScalingLazyColumn(
            state = listState,
            modifier = Modifier
                .fillMaxSize()
                .background(SlateBg),
            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 28.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            // 1. Status row: sync icon and warning dot sit in small fixed side slots
            // that are always present, so the pill never moves (matches Widget).
            item {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.Center
                ) {
                    Box(
                        modifier = Modifier.width(18.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        if (pendingCount > 0) {
                            Icon(
                                painter = painterResource(
                                    if (queueStuck) R.drawable.ic_sync_problem else R.drawable.ic_sync
                                ),
                                contentDescription = if (queueStuck) {
                                    "Punches not syncing"
                                } else {
                                    "Punches queued for sync"
                                },
                                tint = Color.Unspecified,
                                modifier = Modifier.size(11.dp)
                            )
                        }
                    }
                    Box(
                        modifier = Modifier
                            .clip(RoundedCornerShape(999.dp))
                            .background(SlateSurface)
                            .border(
                                1.dp,
                                if (isClockedIn) EmeraldBright.copy(alpha = 0.35f) else SlateBorder,
                                RoundedCornerShape(999.dp)
                            )
                            .padding(horizontal = 12.dp, vertical = 4.dp)
                    ) {
                        Text(
                            text = if (isClockedIn) {
                                "● ${activeLoc.uppercase()}"
                            } else {
                                "○ OFF DUTY"
                            },
                            fontSize = 11.sp,
                            fontWeight = FontWeight.SemiBold,
                            color = if (isClockedIn) EmeraldBright else SlateMuted
                        )
                    }
                    Box(
                        modifier = Modifier.width(18.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        if (warnings.isNotEmpty()) {
                            Box(
                                modifier = Modifier
                                    .size(10.dp)
                                    .clip(CircleShape)
                                    .background(Color(0xFFF59E0B))
                                    .clickable {
                                        scope.launch {
                                            listState.animateScrollToItem(3)
                                        }
                                    }
                            )
                        }
                    }
                }
            }

            // 2. Center Hero Current Duration & Status Line (matches Widget)
            item {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(
                        text = "%02d:%02d".format(shownDurationMin / 60, shownDurationMin % 60),
                        fontSize = 28.sp,
                        fontWeight = FontWeight.Bold,
                        color = SlateText,
                        style = TextStyle(fontFeatureSettings = "tnum"),
                        textAlign = TextAlign.Center
                    )
                    Text(
                        text = "Today %02d:%02d · Break %dm".format(
                            totalTodayMin / 60,
                            totalTodayMin % 60,
                            breakActual
                        ),
                        fontSize = 11.sp,
                        color = SlateMuted,
                        textAlign = TextAlign.Center
                    )
                }
            }

            // 3. Side-by-Side Primary Action Row (matches Widget)
            item {
                if (isClockedIn) {
                    val switchToOffice = activeLoc != "office"
                    val targetLoc = if (switchToOffice) "office" else "remote"
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                        modifier = Modifier.fillMaxWidth(0.94f)
                    ) {
                        Button(
                            onClick = { triggerPunch("out", activeLoc) },
                            enabled = busyAction == null,
                            colors = ButtonDefaults.buttonColors(
                                backgroundColor = RoseAction,
                                contentColor = Color.White
                            ),
                            modifier = Modifier
                                .weight(1.08f)
                                .height(40.dp)
                        ) {
                            if (busyAction?.startsWith("out:") == true) {
                                CircularProgressIndicator(
                                    modifier = Modifier.size(16.dp),
                                    indicatorColor = Color.White
                                )
                            } else {
                                Text("Clock Out", fontSize = 12.sp, fontWeight = FontWeight.Bold)
                            }
                        }

                        Button(
                            onClick = { triggerPunch("switch_location", targetLoc) },
                            enabled = busyAction == null,
                            colors = ButtonDefaults.buttonColors(
                                backgroundColor = Color(0xFF334155),
                                contentColor = SlateText
                            ),
                            modifier = Modifier
                                .weight(0.92f)
                                .height(40.dp)
                        ) {
                            if (busyAction?.startsWith("switch_location:") == true) {
                                CircularProgressIndicator(modifier = Modifier.size(14.dp))
                            } else {
                                Text(
                                    text = if (switchToOffice) "→ Office" else "→ Remote",
                                    fontSize = 12.sp,
                                    fontWeight = FontWeight.Bold
                                )
                            }
                        }
                    }
                } else {
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        modifier = Modifier.fillMaxWidth(0.92f)
                    ) {
                        Button(
                            onClick = { triggerPunch("in", "remote") },
                            enabled = busyAction == null,
                            colors = ButtonDefaults.buttonColors(
                                backgroundColor = TealPrimary,
                                contentColor = Color.White
                            ),
                            modifier = Modifier
                                .weight(1f)
                                .height(40.dp)
                        ) {
                            if (busyAction == "in:remote") {
                                CircularProgressIndicator(
                                    modifier = Modifier.size(16.dp),
                                    indicatorColor = Color.White
                                )
                            } else {
                                Text("Remote", fontSize = 12.5.sp, fontWeight = FontWeight.Bold)
                            }
                        }

                        Button(
                            onClick = { triggerPunch("in", "office") },
                            enabled = busyAction == null,
                            colors = ButtonDefaults.buttonColors(
                                backgroundColor = IndigoAccent,
                                contentColor = Color.White
                            ),
                            modifier = Modifier
                                .weight(1f)
                                .height(40.dp)
                        ) {
                            if (busyAction == "in:office") {
                                CircularProgressIndicator(
                                    modifier = Modifier.size(16.dp),
                                    indicatorColor = Color.White
                                )
                            } else {
                                Text("Office", fontSize = 12.5.sp, fontWeight = FontWeight.Bold)
                            }
                        }
                    }
                }
            }

            // 4. Below-the-fold: Full Warnings, Offline Queue & Sync/Unpair
            if (warnings.isNotEmpty()) {
                warnings.forEach { warnText ->
                    item {
                        Box(
                            modifier = Modifier
                                .fillMaxWidth(0.92f)
                                .clip(RoundedCornerShape(14.dp))
                                .background(AmberWarn)
                                .padding(horizontal = 12.dp, vertical = 8.dp)
                        ) {
                            Text(
                                text = warnText,
                                fontSize = 11.5.sp,
                                color = Color.White,
                                textAlign = TextAlign.Center,
                                modifier = Modifier.fillMaxWidth()
                            )
                        }
                    }
                }
            }

            if (pendingCount > 0) {
                item {
                    CompactChip(
                        onClick = { scope.launch { refreshStatus() } },
                        label = { Text("Queued offline ($pendingCount) · Sync", fontSize = 11.sp) },
                        colors = ChipDefaults.chipColors(backgroundColor = AmberWarn, contentColor = Color.White)
                    )
                }
            }

            if (errorMsg != null) {
                item {
                    Text(
                        text = errorMsg!!,
                        color = Color(0xFFFCA5A5),
                        fontSize = 11.sp,
                        textAlign = TextAlign.Center
                    )
                }
            }

            item {
                Row(
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.padding(top = 4.dp)
                ) {
                    CompactChip(
                        onClick = { scope.launch { refreshStatus() } },
                        label = { Text("Sync", fontSize = 11.sp) },
                        colors = ChipDefaults.chipColors(backgroundColor = SlateSurface, contentColor = SlateText)
                    )
                    CompactChip(
                        onClick = onUnpaired,
                        label = { Text("Unpair", fontSize = 11.sp) },
                        colors = ChipDefaults.chipColors(backgroundColor = SlateSurface, contentColor = SlateMuted)
                    )
                }
            }
        }
    }
}

private fun computeElapsedMinutes(dateStr: String, startHm: String): Int {
    return try {
        val d = LocalDate.parse(dateStr)
        val t = LocalTime.parse(startHm)
        val startDt = LocalDateTime.of(d, t)
        Duration.between(startDt, LocalDateTime.now()).toMinutes().toInt().coerceAtLeast(0)
    } catch (_: Exception) {
        0
    }
}
