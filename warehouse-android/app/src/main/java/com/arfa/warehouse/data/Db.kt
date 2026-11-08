package com.arfa.warehouse.data

import android.content.Context
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import kotlinx.coroutines.flow.Flow

/** An order as last read from the server (its lines and allowed serials),
 * so it opens and scans with no signal. */
@Entity(tableName = "orders")
data class CachedOrder(@PrimaryKey val id: Long, val json: String, val savedAt: Long)

/** A scan waiting to reach the server, or its answer. kind: "pick" (an
 * order line), "unit" (a receipt line), "count" (a stock count). */
@Entity(tableName = "outbox")
data class Pending(
    @PrimaryKey val clientId: String,
    val kind: String,
    val parentId: Long,
    val lineId: Long,
    val serial: String,
    val method: String,
    val allowNew: Boolean = false,
    val photoPath: String? = null,
    val photoSent: Boolean = false,
    val state: String = "queued",
    val message: String? = null,
    val serverId: Long? = null,
    val createdAt: Long = System.currentTimeMillis(),
)

@Dao
interface OrderDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun save(order: CachedOrder)

    @Query("SELECT * FROM orders WHERE id = :id")
    suspend fun get(id: Long): CachedOrder?

    @Query("DELETE FROM orders WHERE savedAt < :before")
    suspend fun prune(before: Long)
}

@Dao
interface OutboxDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun add(item: Pending)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun put(item: Pending)

    @Query("SELECT * FROM outbox WHERE state = 'queued' ORDER BY createdAt")
    suspend fun queued(): List<Pending>

    @Query("SELECT * FROM outbox WHERE state = 'confirmed' AND photoPath IS NOT NULL AND photoSent = 0 AND serverId IS NOT NULL")
    suspend fun photosToSend(): List<Pending>

    @Query("SELECT * FROM outbox WHERE kind = :kind AND parentId = :parentId ORDER BY createdAt")
    fun watch(kind: String, parentId: Long): Flow<List<Pending>>

    @Query("SELECT COUNT(*) FROM outbox WHERE state = 'queued'")
    fun waitingCount(): Flow<Int>

    @Query("DELETE FROM outbox WHERE clientId = :clientId")
    suspend fun remove(clientId: String)

    @Query("DELETE FROM outbox WHERE state != 'queued' AND createdAt < :before")
    suspend fun prune(before: Long)
}

@Database(entities = [CachedOrder::class, Pending::class], version = 1, exportSchema = false)
abstract class AppDb : RoomDatabase() {
    abstract fun orders(): OrderDao
    abstract fun outbox(): OutboxDao

    companion object {
        @Volatile private var instance: AppDb? = null

        fun get(context: Context): AppDb = instance ?: synchronized(this) {
            instance ?: Room.databaseBuilder(context.applicationContext, AppDb::class.java, "warehouse.db")
                .fallbackToDestructiveMigration()
                .build().also { instance = it }
        }
    }
}
