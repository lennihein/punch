package com.lennihein.atoss.wear.ui

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.wear.tiles.TileService
import com.lennihein.atoss.wear.data.TokenStore
import com.lennihein.atoss.wear.tile.PunchTileService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            // Construction is cheap now; the Keystore is only touched on IO threads.
            val store = remember { TokenStore(applicationContext) }
            LaunchedEffect(Unit) { withContext(Dispatchers.IO) { store.warmUp() } }
            WearApp(store = store)
        }
    }

    override fun onResume() {
        super.onResume()
        TileService.getUpdater(applicationContext).requestUpdate(PunchTileService::class.java)
    }
}
