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
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.arfa.warehouse.data.DeliverUnit

/** The delivery check: every unit leaving must be an invoiced serial. A
 * damaged box is swapped only with the owner's approval (the invoice stays;
 * the delivery slip records it). */
@Composable
fun DeliverScreen(vm: AppViewModel, orderId: Long, busy: Boolean) {
    val detail by vm.order.collectAsState()
    var swapFor by remember { mutableStateOf<DeliverUnit?>(null) }
    val d = detail
    Scaffold(topBar = { WarehouseBar("Deliver ${d?.card?.invoice ?: ""}", d?.card?.customer, onBack = { vm.back() }) }) { pad ->
        if (d == null || d.card.id != orderId) {
            Box(Modifier.padding(pad).fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
            return@Scaffold
        }
        val left = d.deliver.count { !it.checked }
        Column(Modifier.padding(pad).fillMaxSize()) {
            if (d.card.emi) Text("EMI · ${d.card.financier ?: ""} - the invoiced serial has gone to the financier",
                color = androidx.compose.ui.graphics.Color.White, fontWeight = FontWeight.SemiBold,
                modifier = Modifier.fillMaxWidth().background(Bad).padding(10.dp))
            d.pendingSwaps.forEach {
                Text("Swap waiting for the owner: ${it.old} → ${it.new} (${it.reason})", color = Warn,
                    fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(12.dp))
            }
            LazyColumn(Modifier.weight(1f), contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                items(d.deliver, key = { it.serial }) { u ->
                    Card(Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(14.dp)) {
                            Text(u.product, fontWeight = FontWeight.SemiBold)
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text((if (u.checked) "✓ " else "") + u.serial, fontSize = 24.sp, fontWeight = FontWeight.Bold,
                                    fontFamily = FontFamily.Monospace, color = if (u.checked) Good else MaterialTheme.colorScheme.onSurface,
                                    modifier = Modifier.weight(1f))
                            }
                            if (u.rack.isNotEmpty()) Text("Fetch from " + when (u.rack) {
                                "EMI-HOLD" -> "the EMI HOLD spot"
                                "EMI-READY" -> "the EMI READY spot"
                                else -> "rack ${u.rack}"
                            }, color = MaterialTheme.colorScheme.secondary, fontWeight = FontWeight.SemiBold)
                            if (!u.checked && d.pendingSwaps.none { it.old == u.serial }) {
                                TextButton({ swapFor = u }) { Text("Box damaged? Ask for a swap") }
                            }
                        }
                    }
                }
            }
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Row {
                    Button({ vm.go(Screen.Scan(ScanTarget.Deliver(orderId))) }, Modifier.weight(1f).height(56.dp)) {
                        Text("Scan units", fontSize = 18.sp, fontWeight = FontWeight.Bold)
                    }
                    Spacer(Modifier.width(10.dp))
                    OutlinedButton({ vm.go(Screen.Scan(ScanTarget.Locate(orderId))) }, Modifier.height(56.dp)) { Text("Locate") }
                }
                Button({ vm.deliver(orderId) }, enabled = left == 0 && d.pendingSwaps.isEmpty() && !busy,
                    modifier = Modifier.fillMaxWidth().height(56.dp)) {
                    Text(if (left == 0) "Delivered - hand over the slip" else "$left unit${if (left == 1) "" else "s"} still to check",
                        fontSize = 17.sp, fontWeight = FontWeight.Bold)
                }
            }
        }
    }
    swapFor?.let { unit ->
        var replacement by remember { mutableStateOf("") }
        var reason by remember { mutableStateOf("") }
        AlertDialog(onDismissRequest = { swapFor = null },
            title = { Text("Swap ${unit.serial}?") },
            text = {
                Column {
                    Text("Type or scan the replacement unit's serial. The owner approves it on the website; the invoice stays and the delivery slip shows the swap.")
                    OutlinedTextField(replacement, { replacement = it.uppercase() }, label = { Text("Replacement serial") },
                        singleLine = true, modifier = Modifier.padding(top = 8.dp))
                    OutlinedTextField(reason, { reason = it }, label = { Text("Why (box damaged, faulty...)") },
                        modifier = Modifier.padding(top = 8.dp))
                }
            },
            confirmButton = {
                Button({ vm.requestSwap(orderId, unit.serial, replacement, reason); swapFor = null },
                    enabled = replacement.isNotBlank() && reason.isNotBlank()) { Text("Ask the owner") }
            },
            dismissButton = { TextButton({ swapFor = null }) { Text("Cancel") } })
    }
}
