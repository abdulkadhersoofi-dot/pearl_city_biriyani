package com.arfa.warehouse.ui

import androidx.compose.foundation.background
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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.arfa.warehouse.data.Line
import com.arfa.warehouse.data.Pending
import com.arfa.warehouse.sync.QueueRules

@Composable
fun OrderScreen(vm: AppViewModel, orderId: Long, busy: Boolean) {
    val detail by vm.order.collectAsState()
    val offline by vm.orderOffline.collectAsState()
    val queue by remember(orderId) { vm.queueFor(orderId) }.collectAsState(initial = emptyList())
    var cantFind by remember { mutableStateOf<Line?>(null) }
    var releasing by remember { mutableStateOf(false) }
    val d = detail
    Scaffold(topBar = {
        WarehouseBar(d?.card?.number ?: "Order", d?.card?.customer, onBack = { vm.back() })
    }) { pad ->
        if (d == null || d.card.id != orderId) {
            Box(Modifier.padding(pad).fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
            return@Scaffold
        }
        val rules = queue.map { QueueRules.Pick(it.clientId, it.lineId, it.serial, it.state, it.message) }
        val views = d.lines.filter { it.needsScan }.map { QueueRules.line(it.qty, it.picked, rules, it.id) }
        val waiting = QueueRules.waiting(rules)
        val mine = d.card.mine && (d.card.status == "picking" || d.card.status == "picked")
        val canConfirm = mine && QueueRules.canConfirm(views, rules) && d.card.status == "picking"
        Column(Modifier.padding(pad).fillMaxSize()) {
            if (offline) Text("No signal - showing this order as last opened. Scans are kept and sent later.",
                color = Color.White, modifier = Modifier.fillMaxWidth().background(Warn).padding(10.dp))
            if (waiting > 0) SyncBanner(waiting)
            LazyColumn(Modifier.weight(1f), contentPadding = PaddingValues(12.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)) {
                items(d.lines, key = { it.id }) { line ->
                    LineCard(vm, orderId, line, queue.filter { it.lineId == line.id }, mine,
                        onCantFind = { cantFind = line })
                }
                if (d.notes.isNotBlank()) item {
                    Text("Notes: ${d.notes}", color = MaterialTheme.colorScheme.secondary, modifier = Modifier.padding(4.dp))
                }
            }
            if (mine && d.card.status == "picking") {
                Column(Modifier.padding(12.dp)) {
                    Button({ vm.confirm(orderId) }, enabled = canConfirm && !busy,
                        modifier = Modifier.fillMaxWidth().height(60.dp)) {
                        Text(when {
                            canConfirm -> "Confirm - send for invoicing"
                            waiting > 0 -> "$waiting pick${if (waiting == 1) "" else "s"} still syncing"
                            else -> {
                                val left = views.sumOf { maxOf(0, it.qty - it.confirmed) }
                                "$left unit${if (left == 1) "" else "s"} still to scan"
                            }
                        }, fontSize = 18.sp, fontWeight = FontWeight.Bold)
                    }
                    TextButton({ releasing = true }, Modifier.fillMaxWidth()) { Text("Release - put it back in the pipeline") }
                }
            } else if (d.card.status == "picked") {
                Text("Sent to the warehouse computer for invoicing.", color = Good, fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.padding(16.dp))
            }
        }
    }
    cantFind?.let { line ->
        var reason by remember { mutableStateOf("") }
        AlertDialog(onDismissRequest = { cantFind = null },
            title = { Text("Can't find ${line.description}?") },
            text = {
                Column {
                    Text("The showroom gets a note on the order.")
                    OutlinedTextField(reason, { reason = it }, label = { Text("What happened (optional)") },
                        modifier = Modifier.padding(top = 8.dp))
                }
            },
            confirmButton = { Button({ vm.cantFind(orderId, line.id, reason); cantFind = null }) { Text("Tell the showroom") } },
            dismissButton = { TextButton({ cantFind = null }) { Text("Cancel") } })
    }
    if (releasing) AlertDialog(onDismissRequest = { releasing = false },
        title = { Text("Release this order?") },
        text = { Text("It goes back to the pipeline for anyone to pick, and your scans are undone.") },
        confirmButton = { Button({ releasing = false; vm.release(orderId) },
            colors = ButtonDefaults.buttonColors(containerColor = Bad)) { Text("Release") } },
        dismissButton = { TextButton({ releasing = false }) { Text("Keep picking") } })
}

@Composable
fun LineCard(vm: AppViewModel, orderId: Long, line: Line, queue: List<Pending>, mine: Boolean, onCantFind: () -> Unit) {
    val view = QueueRules.line(line.qty, line.picked,
        queue.map { QueueRules.Pick(it.clientId, it.lineId, it.serial, it.state) }, line.id)
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp)) {
            Text(line.description, fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
            val model = line.product?.modelNo.orEmpty()
            if (model.isNotEmpty()) Text(model, fontSize = 28.sp, fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace)
            if (line.needsScan) {
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 4.dp)) {
                    Text("${view.shown} of ${line.qty} scanned", fontSize = 18.sp,
                        color = if (view.confirmed >= line.qty) Good else MaterialTheme.colorScheme.onSurface,
                        fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                    if (view.waiting > 0) Text("${view.waiting} syncing", color = Warn)
                }
                line.picks.filter { it.ok }.forEach { p ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("✓ ${p.serial.ifEmpty { "unit" }}", fontFamily = FontFamily.Monospace, color = Good,
                            modifier = Modifier.weight(1f))
                        if (mine) TextButton({ vm.undo(p.id) }) { Text("Undo") }
                    }
                }
                queue.filter { it.state != QueueRules.CONFIRMED }.forEach { q ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        val refused = q.state == QueueRules.REJECTED
                        Text((if (refused) "✗ " else "… ") + q.serial + (if (refused) " - ${q.message ?: "refused"}" else ""),
                            fontFamily = FontFamily.Monospace, color = if (refused) Bad else Warn, modifier = Modifier.weight(1f))
                        if (mine) TextButton({ vm.undoQueued(q.clientId) }) { Text(if (refused) "Clear" else "Undo") }
                    }
                }
                if (mine && view.needsScan) {
                    Row(Modifier.padding(top = 8.dp)) {
                        Button({ vm.go(Screen.Scan(ScanTarget.Pick(orderId, line.id))) },
                            Modifier.weight(1f).height(54.dp)) { Text("Scan", fontSize = 18.sp, fontWeight = FontWeight.Bold) }
                        Spacer(Modifier.width(10.dp))
                        OutlinedButton(onCantFind, Modifier.height(54.dp)) { Text("Can't find") }
                    }
                }
            } else {
                Text("${line.qty} - nothing to scan", color = MaterialTheme.colorScheme.secondary)
            }
        }
    }
}
