package com.arfa.warehouse

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Snackbar
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.arfa.warehouse.ui.AppViewModel
import com.arfa.warehouse.ui.ChoosePinScreen
import com.arfa.warehouse.ui.CountScreen
import com.arfa.warehouse.ui.HomeScreen
import com.arfa.warehouse.ui.OrderScreen
import com.arfa.warehouse.ui.ReceiptScreen
import com.arfa.warehouse.ui.ScanScreen
import com.arfa.warehouse.ui.Screen
import com.arfa.warehouse.ui.SettingsScreen
import com.arfa.warehouse.ui.SignInScreen
import com.arfa.warehouse.ui.UnlockScreen
import com.arfa.warehouse.ui.WarehouseTheme
import kotlinx.coroutines.delay

class MainActivity : ComponentActivity() {

    private val vm: AppViewModel by viewModels()
    private val askNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (!vm.back()) {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                    isEnabled = true
                }
            }
        })
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) {
            askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        setContent { WarehouseTheme { Root(vm) } }
    }

    override fun onResume() {
        super.onResume()
        vm.resumed()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        vm.refreshCurrent()
    }

    override fun onUserInteraction() {
        super.onUserInteraction()
        vm.touched()
    }

    companion object {
        const val EXTRA_OPEN = "open"
        const val EXTRA_ORDER = "order_id"
    }
}

@Composable
private fun Root(vm: AppViewModel) {
    val screen by vm.screen.collectAsState()
    val busy by vm.busy.collectAsState()
    val message by vm.message.collectAsState()
    val update by vm.update.collectAsState()
    val waiting by vm.waiting.collectAsState(initial = 0)
    val app = vm.getApplication<WarehouseApp>()
    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Box(Modifier.fillMaxSize()) {
            when (val s = screen) {
                Screen.Loading -> Unit
                Screen.SignIn -> SignInScreen(vm, busy, app.session.businessCode, app.session.loginId)
                Screen.ChoosePin -> ChoosePinScreen(vm, busy)
                Screen.Pin -> UnlockScreen(vm, busy, app.session.loginName)
                is Screen.Home -> HomeScreen(vm, s.tab, waiting)
                is Screen.Order -> OrderScreen(vm, s.id, busy)
                is Screen.Scan -> ScanScreen(vm, s.target)
                is Screen.ReceiptView -> ReceiptScreen(vm, s.id)
                is Screen.CountView -> CountScreen(vm, s.id)
                Screen.Settings -> SettingsScreen(vm)
            }
            message?.let { text ->
                LaunchedEffect(text) { delay(4500); vm.say(null) }
                Snackbar(Modifier.align(Alignment.BottomCenter).padding(12.dp),
                    action = { TextButton({ vm.say(null) }) { Text("OK") } }) { Text(text) }
            }
        }
    }
    update?.let { (name, link) ->
        val context = androidx.compose.ui.platform.LocalContext.current
        AlertDialog(onDismissRequest = { vm.update.value = null },
            title = { Text("A new ARFA Warehouse is ready") },
            text = { Text("Version $name (you have ${BuildConfig.VERSION_NAME}). It installs over this one - nothing is lost.") },
            confirmButton = {
                Button({
                    vm.update.value = null
                    runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(link))) }
                }) { Text("Download") }
            },
            dismissButton = { TextButton({ vm.update.value = null }) { Text("Later") } })
    }
}
