package com.arfa.warehouse.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.arfa.warehouse.R

@Composable
fun SignInScreen(vm: AppViewModel, busy: Boolean, code0: String, login0: String) {
    var code by rememberSaveable { mutableStateOf(code0) }
    var login by rememberSaveable { mutableStateOf(login0) }
    var password by remember { mutableStateOf("") }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).imePadding().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Spacer(Modifier.height(32.dp))
        Image(painterResource(R.drawable.ic_arfa_logo), null, Modifier.size(84.dp).clip(CircleShape))
        Spacer(Modifier.height(12.dp))
        Text("ARFA Warehouse", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
        Text("Sign in once on this phone. The owner gives you the business code, your login id and password " +
            "(Warehouse - App logins on the website).", textAlign = TextAlign.Center,
            style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.secondary)
        Spacer(Modifier.height(24.dp))
        OutlinedTextField(code, { code = it }, label = { Text("Business code") }, singleLine = true,
            modifier = Modifier.fillMaxWidth())
        OutlinedTextField(login, { login = it }, label = { Text("Login id") }, singleLine = true,
            modifier = Modifier.fillMaxWidth().padding(top = 8.dp))
        OutlinedTextField(password, { password = it }, label = { Text("Password") }, singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
            modifier = Modifier.fillMaxWidth().padding(top = 8.dp))
        Button({ vm.signIn(code, login, password) }, enabled = !busy && code.isNotBlank() && login.isNotBlank() &&
            password.isNotEmpty(), modifier = Modifier.fillMaxWidth().padding(top = 20.dp).height(56.dp)) {
            Text(if (busy) "Signing in..." else "Sign in", fontSize = 18.sp)
        }
    }
}

/** A big number pad - easy with gloves. */
@Composable
fun PinPad(title: String, hint: String, busy: Boolean, onPin: (String) -> Unit, footer: @Composable () -> Unit = {}) {
    var pin by remember { mutableStateOf("") }
    LaunchedEffect(busy) { if (!busy) pin = "" }
    Column(Modifier.fillMaxSize().padding(20.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Spacer(Modifier.height(24.dp))
        Text(title, style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
        Text(hint, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.secondary,
            textAlign = TextAlign.Center)
        Spacer(Modifier.height(20.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            repeat(6) { i ->
                Box(Modifier.size(18.dp).clip(CircleShape)
                    .background(if (i < pin.length) Green else Color.Gray.copy(alpha = 0.35f)))
            }
        }
        Spacer(Modifier.weight(1f))
        val keys = listOf("1", "2", "3", "4", "5", "6", "7", "8", "9", "<", "0", "OK")
        keys.chunked(3).forEach { row ->
            Row(Modifier.fillMaxWidth().padding(vertical = 5.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                row.forEach { key ->
                    FilledTonalButton(
                        onClick = {
                            when (key) {
                                "<" -> pin = pin.dropLast(1)
                                "OK" -> if (pin.length >= 4) onPin(pin)
                                else -> if (pin.length < 6) pin += key
                            }
                        },
                        enabled = !busy && (key != "OK" || pin.length >= 4),
                        modifier = Modifier.weight(1f).aspectRatio(1.6f),
                    ) { Text(if (key == "<") "⌫" else key, fontSize = 28.sp, fontWeight = FontWeight.SemiBold) }
                }
            }
        }
        footer()
    }
}

@Composable
fun UnlockScreen(vm: AppViewModel, busy: Boolean, name: String) {
    PinPad("Hello${if (name.isNotEmpty()) ", $name" else ""}", "Enter your PIN", busy, { vm.unlock(it) }) {
        TextButton({ vm.signOut() }, Modifier.padding(top = 8.dp)) { Text("Not you? Sign in with a password") }
    }
}

@Composable
fun ChoosePinScreen(vm: AppViewModel, busy: Boolean) {
    var first by remember { mutableStateOf<String?>(null) }
    if (first == null) {
        PinPad("Choose a PIN", "4 to 6 digits. You open the app with it.", busy, { first = it })
    } else {
        PinPad("Enter it again", "The same PIN once more.", busy, { vm.choosePin(first!!, it); first = null })
    }
}
