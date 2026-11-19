package com.lennihein.atoss.wear.tile

import android.os.Build
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.wear.tiles.TileService
import com.lennihein.atoss.wear.data.TokenStore
import com.lennihein.atoss.wear.network.AtossApiClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class TileActionActivity : ComponentActivity() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val action = intent.getStringExtra(EXTRA_ACTION) ?: "toggle"
        val location = intent.getStringExtra(EXTRA_LOCATION) ?: "remote"
        val store = TokenStore(applicationContext)
        val token = store.token

        if (token.isNullOrBlank()) {
            Toast.makeText(this, "Pair watch first", Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        scope.launch {
            try {
                val updated = AtossApiClient.performOrQueuePunch(
                    context = applicationContext,
                    store = store,
                    action = action,
                    location = location
                )
                TileService.getUpdater(applicationContext).requestUpdate(PunchTileService::class.java)
                vibrateConfirm()
                val queued = store.getPendingPunches().size
                val label = if (queued > 0) {
                    if (updated.clockedIn) "Queued in ($location)" else "Queued out"
                } else if (updated.clockedIn) {
                    "Clocked in (${updated.activeEntry?.location ?: location})"
                } else {
                    "Clocked out"
                }
                withContext(Dispatchers.Main) {
                    Toast.makeText(applicationContext, label, Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    Toast.makeText(applicationContext, e.message ?: "Punch failed", Toast.LENGTH_SHORT).show()
                }
            } finally {
                finish()
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun vibrateConfirm() {
        val vibrator = getSystemService(Vibrator::class.java) ?: return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            vibrator.vibrate(VibrationEffect.createOneShot(45L, VibrationEffect.DEFAULT_AMPLITUDE))
        } else {
            vibrator.vibrate(45L)
        }
    }

    companion object {
        const val EXTRA_ACTION = "action"
        const val EXTRA_LOCATION = "location"
    }
}
