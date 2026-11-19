package com.lennihein.atoss.wear.ui

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.wear.tiles.TileService
import com.lennihein.atoss.wear.data.TokenStore
import com.lennihein.atoss.wear.tile.PunchTileService

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = TokenStore(applicationContext)
        setContent {
            WearApp(store = store)
        }
    }

    override fun onResume() {
        super.onResume()
        TileService.getUpdater(applicationContext).requestUpdate(PunchTileService::class.java)
    }
}
