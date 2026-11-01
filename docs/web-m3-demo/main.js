import {App} from "./app.js";
App(document.querySelector("#app"),{endpoint:new URL("./products.json",location.href).pathname,writable:false});
