/** 确认 ./ga 启动的 UI 进程里 GA_INK_MOUSE 是什么值、mouseMode 解析成什么。 */
import { resolveMouseCaptureMode } from '../src/mouseWheel.ts'

console.log('env.GA_INK_MOUSE =', JSON.stringify(process.env.GA_INK_MOUSE))
console.log('resolveMouseCaptureMode(process.env) =', resolveMouseCaptureMode(process.env))
console.log('resolveMouseCaptureMode({}) =', resolveMouseCaptureMode({}))
