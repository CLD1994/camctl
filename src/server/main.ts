import { resolve } from 'node:path';
import { Application } from './application';
import { Files } from './files';
import { createHttpApp } from './http';

const directory=resolve(process.env.CAMCTL_DATA_DIR??'data');
const port=Number(process.env.PORT??4310);
if(!Number.isInteger(port)||port<1||port>65535)throw new Error('PORT 必须是 1～65535 的整数');
const application=new Application(directory);
const files=new Files(application);
await files.recover();
const host=process.env.HOST??'127.0.0.1';
const server=createHttpApp(application,files).listen(port,host,()=>{
  console.log(`camctl 客户端：http://localhost:${port}`);
  console.log(`持久化目录：${directory}`);
});
let stopping=false;
async function stop(){if(stopping)return;stopping=true;server.close();await files.idle();application.store.close();}
process.on('SIGINT',()=>void stop());process.on('SIGTERM',()=>void stop());
