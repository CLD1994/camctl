import { DatabaseSync } from 'node:sqlite';
import { accessSync, constants, readdirSync, statSync, openSync, closeSync } from 'node:fs';
import { join, resolve } from 'node:path';

export type Startup = {state:'uninitialized'|'ready'|'fault'; directory:string; message?:string};
export interface SavedReport { report_id:number; file_name:string; bytes:Uint8Array; from_wm:number; to_wm:number }
export class DataError extends Error { readonly code='data_fault'; }

/** 每个服务进程持有一个连接；事务函数必须同步，避免事务跨越外部副作用。 */
export class Store {
  readonly directory: string;
  private connection?: DatabaseSync;
  constructor(directory:string) { this.directory=resolve(directory); }
  get filename() { return join(this.directory,'client.db'); }
  status():Startup {
    try {
      if (!statSync(this.directory).isDirectory()) throw new DataError('数据位置不是目录');
      accessSync(this.directory,constants.R_OK|constants.W_OK);
      const entries=readdirSync(this.directory);
      if (!entries.includes('client.db')) {
        if(this.connection) throw new DataError('正在使用的数据库文件缺失');
        if(entries.some(e=>e!=='device-capabilities.json')) throw new DataError('数据库缺失，但目录包含既有或无法解释的数据');
        return {state:'uninitialized',directory:this.directory};
      }
      this.openExisting();
      return {state:'ready',directory:this.directory};
    } catch(error) { return {state:'fault',directory:this.directory,message:error instanceof Error?error.message:String(error)}; }
  }
  private openExisting():DatabaseSync {
    if(this.connection) return this.connection;
    let db:DatabaseSync|undefined;
    try {
      // 先以只读方式检查，SQLite 不得因连接而创建缺失的库。
      db=new DatabaseSync(this.filename,{readOnly:true});
      const check=db.prepare('PRAGMA quick_check').get();
      if(!check || Object.values(check)[0]!=='ok') throw new DataError('数据库完整性检查失败');
      const version=db.prepare('SELECT value FROM metadata WHERE key=?').get('schema_version');
      if(version?.value!=='1') throw new DataError('数据库尚未完整初始化或版本不受支持');
      db.prepare('SELECT namespace,id,value FROM records LIMIT 0').all();
      db.prepare('SELECT report_id,file_name,bytes,from_wm,to_wm FROM reports LIMIT 0').all();
      db.close(); db=undefined;
      const fd=openSync(this.filename,'r+'); closeSync(fd);
      db=new DatabaseSync(this.filename,{timeout:5000});
      db.exec('PRAGMA foreign_keys=ON; PRAGMA synchronous=FULL;');
      this.connection=db;
      return db;
    } catch(error) { db?.close(); throw new DataError(`无法读取客户端数据库：${error instanceof Error?error.message:String(error)}`); }
  }
  initialize():Startup {
    const state=this.status();
    if(state.state==='fault') throw new DataError(state.message);
    if(state.state==='ready') return state;
    const fd=openSync(this.filename,'wx'); closeSync(fd);
    const db=new DatabaseSync(this.filename);
    try {
      db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        BEGIN IMMEDIATE;
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) STRICT;
        CREATE TABLE records(namespace TEXT NOT NULL,id TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(namespace,id)) STRICT;
        CREATE TABLE reports(report_id INTEGER PRIMARY KEY,file_name TEXT NOT NULL UNIQUE,bytes BLOB NOT NULL,from_wm INTEGER NOT NULL,to_wm INTEGER NOT NULL) STRICT;
        INSERT INTO metadata VALUES('schema_version','1');
        COMMIT;`);
    } finally { db.close(); }
    return this.status();
  }
  private db():DatabaseSync {
    if(this.status().state!=='ready') throw new DataError('客户端数据不可用，请检查数据目录和数据库');
    return this.openExisting();
  }
  get<T>(namespace:string,id:string):T|undefined {
    const row=this.db().prepare('SELECT value FROM records WHERE namespace=? AND id=?').get(namespace,id);
    return row ? JSON.parse(row.value as string) as T : undefined;
  }
  all<T>(namespace:string):T[] { return this.db().prepare('SELECT value FROM records WHERE namespace=? ORDER BY rowid').all(namespace).map(row=>JSON.parse(row.value as string) as T); }
  set(namespace:string,id:string,value:unknown):void { this.db().prepare('INSERT INTO records(namespace,id,value) VALUES(?,?,?) ON CONFLICT(namespace,id) DO UPDATE SET value=excluded.value').run(namespace,id,JSON.stringify(value)); }
  transaction<T>(operation:()=>T):T {
    const db=this.db(); db.exec('BEGIN IMMEDIATE');
    try { const result=operation(); if(result instanceof Promise) throw new DataError('数据库事务不能跨越异步操作'); db.exec('COMMIT'); return result; }
    catch(error) { if(db.isTransaction) db.exec('ROLLBACK'); throw error; }
  }
  report(id:number):SavedReport|undefined { return this.db().prepare('SELECT * FROM reports WHERE report_id=?').get(id) as unknown as SavedReport|undefined; }
  reports():Array<Omit<SavedReport,'bytes'>> { return this.db().prepare('SELECT report_id,file_name,from_wm,to_wm FROM reports ORDER BY report_id').all() as unknown as Array<Omit<SavedReport,'bytes'>>; }
  saveReport(id:number,name:string,bytes:Uint8Array,from:number,to:number):void { this.db().prepare('INSERT INTO reports VALUES(?,?,?,?,?)').run(id,name,bytes,from,to); }
  close():void { this.connection?.close(); this.connection=undefined; }
}
