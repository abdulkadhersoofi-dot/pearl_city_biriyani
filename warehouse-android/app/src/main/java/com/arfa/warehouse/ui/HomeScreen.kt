package com.arfa.warehouse.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ButtonDefaults
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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Tab
import androidx.compose.material3.ScrollableTabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.arfa.warehouse.data.OrderCard
import kotlinx.coroutines.delay

const val LATE_MINUTES = 120

fun age(minutes: Int): String = when {
    minutes < 1 -> "just now"
    minutes < 60 -> "$minutes min ago"
    minutes < 24 * 60 -> "${minutes / 60} h ago"
    else -> "${minutes / (24 * 60)} d ago"
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun WarehouseBar(title: String, subtitle: String? = null, onBack: (() -> Unit)? = null,
                 actions: @Composable () -> Unit = {}) {
    TopAppBar(
        title = {
            Column {
                Text(title, fontWeight = FontWeight.Bold, maxLines = 1)
                if (!subtitle.isNullOrEmpty()) Text(subtitle, fontSize = 13.sp, maxLines = 1)
            }
        },
        navigationIcon = { if (onBack != null) TextButton(onBack) { Text("‹ Back", color = Color.White, fontSize = 16.sp) } },
        actions = { actions() },
        colors = TopAppBarDefaults.topAppBarColors(containerColor = Green, titleContentColor = Color.White,
            actionIconContentColor = Color.White),
    )
}

@Composable
fun HomeScreen(vm: AppViewModel, tab: HomeTab, waiting: Int) {
    val orders by vm.orders.collectAsState()
    val session = vm.getApplication<com.arfa.warehouse.WarehouseApp>().session
    val warehouses = session.warehouses()
    var menu by remember { mutableStateOf(false) }
    LaunchedEffect(tab, session.warehouseId) {
        while (true) {                       // the pipeline keeps itself fresh even if a push is lost
            delay(session.pollSeconds * 1000L)
            vm.refresh()
        }
    }
    Scaffold(topBar = {
        WarehouseBar("ARFA Warehouse", session.warehouseName()) {
            if (warehouses.size > 1) {
                Box {
                    TextButton({ menu = true }) { Text("Switch", color = Color.White) }
                    DropdownMenu(menu, { menu = false }) {
                        warehouses.forEach { (id, name) ->
                            DropdownMenuItem(text = { Text(name) }, onClick = { menu = false; vm.chooseWarehouse(id) })
                        }
                    }
                }
            }
            TextButton({ vm.go(Screen.Settings) }) { Text("⚙", color = Color.White, fontSize = 22.sp) }
        }
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize()) {
            ScrollableTabRow(selectedTabIndex = tab.ordinal, edgePadding = 8.dp) {
                listOf("Pipeline", "My picks", "Deliver", "Done today", "Receive").forEachIndexed { i, label ->
                    Tab(selected = tab.ordinal == i, onClick = { vm.tab(HomeTab.entries[i]) }, text = { Text(label, maxLines = 1) })
                }
            }
            if (waiting > 0) SyncBanner(waiting)
            if (tab == HomeTab.RECEIVE) {
                ReceiveList(vm)
            } else if (orders.isEmpty()) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text(when (tab) {
                        HomeTab.PIPELINE -> "No orders waiting.\nA new one rings like a message."
                        HomeTab.MINE -> "You aren't picking anything."
                        HomeTab.DELIVER -> "Nothing invoiced waiting to go out."
                        else -> "Nothing confirmed today yet."
                    }, color = MaterialTheme.colorScheme.secondary, textAlign = androidx.compose.ui.text.style.TextAlign.Center)
                }
            } else {
                LazyColumn(contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    items(orders, key = { it.id }) { card -> OrderCardView(vm, card, tab) }
                }
            }
        }
    }
}

@Composable
fun SyncBanner(waiting: Int) {
    Text("$waiting scan${if (waiting == 1) "" else "s"} still syncing - they'll send when there's signal",
        color = Color.White, fontWeight = FontWeight.SemiBold,
        modifier = Modifier.fillMaxWidth().background(Warn).padding(10.dp))
}

@Composable
fun OrderCardView(vm: AppViewModel, card: OrderCard, tab: HomeTab) {
    val lockedByOther = card.claimedBy != null && !card.mine
    val late = card.ageMinutes >= LATE_MINUTES
    // An EMI sale's running timer: the customer is waiting at the showroom.
    var minutes by remember(card.id, card.ageMinutes) { mutableStateOf(card.ageMinutes) }
    if (card.emi && tab == HomeTab.PIPELINE) {
        LaunchedEffect(card.id, card.ageMinutes) {
            while (true) { delay(60_000); minutes += 1 }
        }
    }
    val emiLive = card.emi && tab != HomeTab.DONE
    Card(
        Modifier.fillMaxWidth()
            .then(if (emiLive) Modifier.border(3.dp, Bad, RoundedCornerShape(12.dp)) else Modifier)
            .clickable(enabled = !lockedByOther) {
                when {
                    tab == HomeTab.DELIVER -> vm.go(Screen.Deliver(card.id))
                    card.mine || tab == HomeTab.DONE -> vm.go(Screen.Order(card.id))
                }
            },
        colors = CardDefaults.cardColors(
            containerColor = if (lockedByOther) MaterialTheme.colorScheme.surfaceVariant else MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = if (lockedByOther) 0.dp else 2.dp),
    ) {
        if (emiLive) {
            Text("EMI · ${card.financier ?: "finance"}" + if (tab == HomeTab.PIPELINE) " · customer waiting $minutes min" else "",
                color = Color.White, fontWeight = FontWeight.Bold,
                modifier = Modifier.fillMaxWidth().background(Bad).padding(horizontal = 14.dp, vertical = 6.dp))
        }
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(card.number, fontWeight = FontWeight.Bold, fontSize = 18.sp, modifier = Modifier.weight(1f))
                Text(age(card.ageMinutes), color = if (late && tab == HomeTab.PIPELINE) Bad else MaterialTheme.colorScheme.secondary,
                    fontWeight = if (late) FontWeight.Bold else FontWeight.Normal)
            }
            Text("${card.customer} · ${card.branch}${if (card.salesperson.isNotEmpty() && card.salesperson != card.branch) " · " + card.salesperson else ""}",
                color = MaterialTheme.colorScheme.secondary)
            Spacer(Modifier.height(6.dp))
            card.items.forEach { (desc, qty) -> Text("$qty × $desc", fontSize = 15.sp) }
            Spacer(Modifier.height(10.dp))
            when {
                tab == HomeTab.DELIVER -> Button({ vm.go(Screen.Deliver(card.id)) }, Modifier.fillMaxWidth().height(54.dp)) {
                    Text("Deliver ${card.invoice ?: ""} - scan each unit", fontSize = 17.sp, fontWeight = FontWeight.Bold)
                }
                tab == HomeTab.DONE -> Text(when (card.status) {
                    "delivered" -> "Delivered"
                    "invoiced" -> "Invoiced - waiting for the delivery check"
                    else -> if (card.emi) "Locked for EMI - the showroom invoices" else "At the computer for invoicing"
                }, color = Good, fontWeight = FontWeight.SemiBold)
                lockedByOther -> Text("Picking: ${card.claimedBy}", fontWeight = FontWeight.SemiBold,
                    color = MaterialTheme.colorScheme.secondary)
                card.mine -> OutlinedButton({ vm.go(Screen.Order(card.id)) }, Modifier.fillMaxWidth().height(52.dp)) {
                    Text("Continue picking (${card.picked}/${card.units})", fontSize = 17.sp)
                }
                else -> Button({ vm.claim(card.id) }, Modifier.fillMaxWidth().height(56.dp),
                    colors = if (card.emi) ButtonDefaults.buttonColors(containerColor = Bad) else ButtonDefaults.buttonColors()) {
                    Text(if (card.emi) "Start picking - EMI" else "Start picking", fontSize = 18.sp, fontWeight = FontWeight.Bold)
                }
            }
        }
    }
}
