package com.arfa.warehouse.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.arfa.warehouse.BuildConfig
import com.arfa.warehouse.WarehouseApp
import com.arfa.warehouse.sync.QueueRules

/** The Receive tab: purchases sent to this warehouse, and counts. */
@Composable
fun ReceiveList(vm: AppViewModel) {
    val receipts by vm.receipts.collectAsState()
    val counts by vm.counts.collectAsState()
    LazyColumn(contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item { Text("Purchases to receive", fontWeight = FontWeight.Bold, fontSize = 17.sp) }
        if (receipts.isEmpty()) item {
            Text("None. When goods arrive, the office sends the purchase order here.",
                color = MaterialTheme.colorScheme.secondary)
        }
        items(receipts, key = { "r" + it.id }) { r ->
            Card(Modifier.fillMaxWidth().clickable { vm.go(Screen.ReceiptView(r.id)) }) {
                Column(Modifier.padding(14.dp)) {
                    Text(r.orderNumber, fontWeight = FontWeight.Bold, fontSize = 18.sp)
                    Text(r.supplier, color = MaterialTheme.colorScheme.secondary)
                    Text("${r.received} / ${r.units} scanned", fontSize = 16.sp, modifier = Modifier.padding(top = 6.dp))
                }
            }
        }
        item {
            HorizontalDivider(Modifier.padding(vertical = 8.dp))
            Text("Count stock", fontWeight = FontWeight.Bold, fontSize = 17.sp)
            Text("Scan every serial on the floor. The website lists what ARFA has that wasn't found, and what was found " +
                "that ARFA doesn't have here.", color = MaterialTheme.colorScheme.secondary)
        }
        items(counts, key = { "c" + it.id }) { c ->
            OutlinedButton({ vm.openCount(c) }, Modifier.fillMaxWidth().height(52.dp)) {
                Text("Carry on counting (${c.scanned} so far${if (c.startedBy.isNotEmpty()) ", " + c.startedBy else ""})")
            }
        }
        item { Button({ vm.startCount() }, Modifier.fillMaxWidth().height(54.dp)) { Text("Start a count", fontSize = 17.sp) } }
        item {
            HorizontalDivider(Modifier.padding(vertical = 8.dp))
            Text("Racks", fontWeight = FontWeight.Bold, fontSize = 17.sp)
            Text("Scan a rack label, then the boxes you put there - each serial then shows its rack, so anyone can find it. " +
                "Keep a few units of fast sellers in EMI READY by the door.", color = MaterialTheme.colorScheme.secondary)
            OutlinedButton({ vm.setRack(null); vm.go(Screen.Scan(ScanTarget.Move)) }, Modifier.fillMaxWidth().padding(top = 8.dp).height(52.dp)) {
                Text("Move units to a rack")
            }
        }
    }
}

@Composable
fun ReceiptScreen(vm: AppViewModel, receiptId: Long) {
    val r by vm.receipt.collectAsState()
    val queue by remember(receiptId) { vm.unitsQueueFor(receiptId) }.collectAsState(initial = emptyList())
    val rack by vm.currentRack.collectAsState()
    val receipt = r
    Scaffold(topBar = { WarehouseBar(receipt?.orderNumber ?: "Receive", receipt?.supplier, onBack = { vm.back() }) }) { pad ->
        if (receipt == null || receipt.id != receiptId) return@Scaffold
        LazyColumn(Modifier.padding(pad).fillMaxSize(), contentPadding = PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)) {
            item {
                Text(rack?.let { "Putting units on rack $it - scan another rack label to change" }
                    ?: "Tip: scan a rack label first, and the units you scan after it are recorded on that rack.",
                    color = MaterialTheme.colorScheme.secondary)
            }
            items(receipt.lines, key = { it.id }) { line ->
                val waiting = queue.count { it.lineId == line.id && it.state == QueueRules.QUEUED }
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(14.dp)) {
                        Text(line.description, fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
                        line.product?.modelNo?.takeIf { it.isNotEmpty() }?.let {
                            Text(it, fontSize = 24.sp, fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace)
                        }
                        Text("${minOf(line.qty, line.received + waiting)} / ${line.qty} scanned", fontSize = 18.sp,
                            fontWeight = FontWeight.SemiBold)
                        queue.filter { it.lineId == line.id && it.state == QueueRules.REJECTED }.forEach {
                            Text("✗ ${it.serial} - ${it.message}", color = Bad, fontFamily = FontFamily.Monospace)
                        }
                        if (line.received + waiting < line.qty) {
                            Button({ vm.go(Screen.Scan(ScanTarget.Unit(receiptId, line.id))) },
                                Modifier.fillMaxWidth().padding(top = 8.dp).height(54.dp)) {
                                Text("Scan units", fontSize = 18.sp, fontWeight = FontWeight.Bold)
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun CountScreen(vm: AppViewModel, countId: Long) {
    val queue by remember(countId) { vm.countQueueFor(countId) }.collectAsState(initial = emptyList())
    val c by vm.count.collectAsState()
    Scaffold(topBar = { WarehouseBar("Count", "${(c?.scanned ?: 0) + queue.size} scanned on this phone", onBack = { vm.back() }) }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Button({ vm.go(Screen.Scan(ScanTarget.Count(countId))) }, Modifier.fillMaxWidth().height(64.dp)) {
                Text("Scan serials", fontSize = 20.sp, fontWeight = FontWeight.Bold)
            }
            OutlinedButton({ vm.finishCount(countId) }, Modifier.fillMaxWidth().height(54.dp)) { Text("Finish the count") }
            Text("Last scanned", fontWeight = FontWeight.Bold)
            Column(Modifier.verticalScroll(rememberScrollState())) {
                queue.reversed().take(50).forEach {
                    Text("${if (it.state == QueueRules.QUEUED) "…" else "✓"} ${it.serial}", fontFamily = FontFamily.Monospace)
                }
            }
        }
    }
}

@Composable
fun SettingsScreen(vm: AppViewModel) {
    val app = LocalContext.current.applicationContext as WarehouseApp
    val session = app.session
    var device by remember { mutableStateOf(session.deviceName) }
    var sound by remember { mutableStateOf(session.sound) }
    var hardware by remember { mutableStateOf(session.hardwareScanner) }
    val context = LocalContext.current
    Scaffold(topBar = { WarehouseBar("Settings", session.loginName, onBack = { vm.back() }) }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp)) {
            OutlinedTextField(device, { device = it; session.deviceName = it }, label = { Text("This phone's name") },
                modifier = Modifier.fillMaxWidth())
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Sound and vibration when scanning", Modifier.weight(1f))
                Switch(sound, { sound = it; session.sound = it })
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Rugged scanner phone")
                    Text("Zebra / Honeywell scan button, or a keyboard-wedge scanner", fontSize = 13.sp,
                        color = MaterialTheme.colorScheme.secondary)
                }
                Switch(hardware, { hardware = it; session.hardwareScanner = it })
            }
            Button({ vm.lock() }, Modifier.fillMaxWidth().height(52.dp)) { Text("Lock now") }
            OutlinedButton({ vm.checkUpdate(true) }, Modifier.fillMaxWidth()) { Text("Check for an update") }
            OutlinedButton({ context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(session.server + "/apps"))) },
                Modifier.fillMaxWidth()) { Text("Open arfa.live/apps") }
            TextButton({ vm.signOut() }, Modifier.fillMaxWidth()) { Text("Sign out of this phone", color = Bad) }
            Text("ARFA Warehouse ${BuildConfig.VERSION_NAME} · ${session.warehouseName()}",
                color = MaterialTheme.colorScheme.secondary, fontSize = 13.sp)
        }
    }
}
