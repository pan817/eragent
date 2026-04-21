 ▐▛███▜▌   Claude Code v2.1.116
▝▜█████▛▘  Opus 4.7 (1M context) · Claude Max
  ▘▘ ▝▝    ~/code/python/erpagent/eragent

❯ <ide_opened_file>The user opened the file /Users/xieguopeng/code/python/erpag 
  ent/eragent/docs/issue/async_analyze_backend_issue copy.md in the IDE. This   
  may or may not be related to the current task.</ide_opened_file>              

❯ 前端分析了一个问题，需要后端核实这个问题是否存在，必须明确给出结论和修复方案  
  ，本次先不修改代码                                                            

  Read 1 file (ctrl+o to expand)

⏺ Explore(Investigate async analyze SSE backend)
  ⎿  Done (25 tool uses · 78.7k tokens · 1m 32s)
  (ctrl+o to expand)

  Searched for 3 patterns, read 8 files (ctrl+o to expand)

⏺ Bash(git log --oneline -n 30 -- api/routes/analyze_async.py 2>&1 | head -30)
  ⎿  b0657ac 异步化改造+bug修复
     f1d96c6 异步化改造+bug修复
     da2db82 异步化改造+bug修复
     dca9f41 异步化改造+bug修复

⏺ Bash(git log --oneline --all -- api/routes/analyze_async.py 2>&1 | head -20)
  ⎿  b0657ac 异步化改造+bug修复
     f1d96c6 异步化改造+bug修复
     da2db82 异步化改造+bug修复
     dca9f41 异步化改造+bug修复

⏺ Bash(git log --all --oneline -S '"type": "heartbeat"' --
      api/routes/analyze_async.py 2>&1 | head -10)
  ⎿  dca9f41 异步化改造+bug修复

⏺ Bash(git log --all --oneline -S 'ts.*now_cn().isoformat' --
      api/routes/analyze_async.py 2>&1 | head -10)
  ⎿  (No output)

⏺ Bash(git log b0657ac --stat -1 -- api/routes/analyze_async.py 2>&1 | head -10
      && echo "---" && git show b0657ac --stat 2>&1 | head -30)
  ⎿  commit b0657ac26871ea197931634242a977f5b48b2533
     Author: pan817 <pan817@live.com>
     Date:   Wed Apr 15 16:31:49 2026 +0800
     … +22 lines (ctrl+o to expand)

⏺ Bash(git show dca9f41:api/routes/analyze_async.py 2>&1 | grep -n
      "heartbeat\|_format_heartbeat" | head -20)
  ⎿  39:def _sse_heartbeat_seconds() -> float:
     41:        return float(get_settings().async_analysis.sse_heartbeat_se
     c)
     … +6 lines (ctrl+o to expand)

⏺ Bash(git show dca9f41:api/routes/analyze_async.py 2>&1 | sed -n '180,230p')
  ⎿  async def stream_task_events(
         trace_id: str,
         request: Request,
     … +23 lines (ctrl+o to expand)

  Searched for 1 pattern, read 1 file (ctrl+o to expand)

❯ 你是否卡住了，必须明确给结论                                                  

⏺ 没卡住，结论如下。

  核实结论

  Issue 1：SSE 只收到 heartbeat —— 历史 bug，当前代码已修复

  - 报告日期 2026-04-14；修复落在 2026-04-15 的 commit
  dca9f41/da2db82/f1d96c6/b0657ac 里
  - 当前 analyze_async.py:220-310 已实现"三道闸"：
    - 闸 1：preflight 校验 trace_id（404 保护）
    - 闸 2：连接后立即 yield _synthesize_status_event（不依赖 buffer replay）
    - 闸 3：退出前若未发过 done，合成 done 或 STREAM_DROPPED
  - Registry 在 QUEUED/RUNNING/done 三处都 publish（registry.py:180,250,320）
  - Middleware 发 stage/tool/dag_task（middleware.py:336-423）
  - heartbeat 缺 ts/seq：历史版本 dca9f41:380 确实只写 type+trace_id；当前
  analyze_async.py:541-546 已补齐 ts+seq=0
  - 残留风险：event_backend=memory + workers>1 时跨 worker 事件送不达，启动时仅
  WARNING（main.py:56-77），不 fail-fast；生产配置 config.yaml:144 默认 memory

  Issue 2：done 先于快照落库 —— 真实存在，已加 2s flush 屏障但残留窗口未闭合

  - registry.py:319 在 _publish_done 前调用 _await_trace_run_flushed（默认 2s
  超时）
  - 超时后只打 WARNING 继续发 done（registry.py:358-363），race window 仍然存在
  - analyze_async.py:418-426 的"belt-and-suspenders"注释自承认："剩余时序残差"
  - 单 worker 同进程路径：registry 先更新
  entry.state、entry.result（registry.py:283-295），snapshot 优先读内存，无 race
  - 跨 worker / TraceStore 未配置 flush API 时：race 窗口确实存在

  Issue 3：chat_messages.status 停在 pending —— 真实存在，覆盖不全

  - 正常失败不会触发：orchestrator.analyze 已 catch 全部异常，返回
  AnalysisResult(status=FAILED)（orchestrator.py:805-833），runner 的
  update_message 会把 status 写成 error
  - 以下路径仍然把 status 留在 pending：
    a. runner 硬超时（registry.py:263-281 600s 后 TimeoutError，直接进 registry
  finally，根本不走 runner 里的 update_message）
    b. CancelledError / shutdown（registry.py:296-304）
    c. sweep 强制纠偏（registry.py:394-447 只改 entry + 发 done，不碰
  chat_messages）
    d. update_message 本身抛异常（analyze_async.py:366-371 仅 WARNING）
  - 兜底只有启动/关闭时的
  _mark_stale_as_aborted（registry.py:503-533），没有周期性 sweep

  修复方案

  修复 1（Issue 1 残留）：event_backend 配错 fail-fast 或强制升级

  - 方案 A：api/main.py:_check_event_backend_matches_workers 把 WARNING 升级为
  RuntimeError，阻止 memory + workers>1 启动
  - 方案 B：生产 config.yaml:144 默认改 redis，memory 仅用于测试

  修复 2（Issue 2）：把"改 chat_messages / 写 reports / 发 done"全部纳入 flush 
  屏障的同一事务窗口

  - 选项 A（推荐）：_await_trace_run_flushed 超时从 WARNING 改为继续 retry /
  阻塞更久（当前 2s，可配到 10s），超时则把 done
  事件降级为"触发前端二次轮询"而不是发假 done
  - 选项 B：在 _publish_done 之前再多一次 chat_messages.status 写入的同步 commit
   确认
  - 选项 C：snapshot 接口对"bus.closed + trace_runs=running"的 race 态直接 block
   读 1-2s 再返回，不要暴露中间态给前端

  修复 3（Issue 3）：把 chat_messages 收尾从 runner 挪到 registry.finally

  把 update_message 从 analyze_async.py:345-371 搬到 TaskRegistry._run 的
  finally 里统一执行：
  finally:
      await flush trace_runs
      update chat_messages (entry.state + content + duration)  <-- 新增
      publish done
  好处：timeout / cancel / sweep / 异常全部覆盖，与 trace_runs 终态同 finally
  原子。另外加一条周期性 sweep 扫 chat_messages.status='pending' AND created_at 
  < now-30min 补为 error（对应前端 P1 诉求）。

  优先级：修复 3 > 修复 2 > 修复 1。要不要进入修复阶段？

❯ 修复                                                                          
⏺ 先看几处关键上下文，然后动刀。

  Searched for 1 pattern (ctrl+o to expand)

  Searched for 4 patterns, read 4 files (ctrl+o to expand)

⏺ 开始实施。

⏺ Update(core/tasks/registry.py)
  ⎿  Added 7 lines
      38  RunnerFactory = Callable[["TaskEntry"], Awaitable[AnalysisResul
          t]]
      39  """把 TaskEntry 转化为可运行协程的工厂；由 API 路由注入具体实现
          。"""
      40  
      41 +Finalizer = Callable[["TaskEntry"], Awaitable[None]]           
      42 +"""任务终态收尾回调：由 API 路由注入，在所有终止路径里统一更新 
         +chat_messages                                                  
      43 +等业务副作用表（success/error/abort/timeout/stall 全覆盖）。reg
         +istry 不直接依赖                                               
      44 +chat_repository，仅通过此回调解耦。"""                         
      45  
      46 +                                                               
      47  @dataclass
      48  class TaskEntry:
      49      """单个异步分析任务的全部运行期信息。"""
     ...
      63      result: AnalysisResult | None = None
      64      error: ErrorInfo | None = None
      65      task: asyncio.Task[Any] | None = None
      66 +    finalizer: Finalizer | None = None                         
      67 +    finalized: bool = False                                    
      68  
      69  
      70  class TaskRegistry:

⏺ Update(core/tasks/registry.py)
  ⎿  Added 5 lines, removed 2 lines
      157          assistant_message_id: str | None,
      158          user_message_id: str | None,
      159          runner_factory: RunnerFactory,
      160 +        finalizer: Finalizer | None = None,                   
      161      ) -> TaskEntry:
      162          """登记并启动一个异步分析任务。
      163  
      163 -        runner_factory 负责实际调用 orchestrator + 持久化 chat
          - 消息；                                                       
      164 -        本方法只管 entry 生命周期与并发控制。                 
      164 +        runner_factory 负责调用 orchestrator.analyze；finalize
          +r 在任务终结                                                  
      165 +        的 finally 链路里统一触发（覆盖 success / FAILED / tim
          +eout / cancel /                                               
      166 +        sweep-stall 所有路径），用于把 pending 态的 chat_messa
          +ges 落成终态。                                                
      167          """
      168          if self._closed:
      169              raise RuntimeError("task registry has been shut do
           wn")
     ...
      175              request=request,
      176              assistant_message_id=assistant_message_id,
      177              user_message_id=user_message_id,
      178 +            finalizer=finalizer,                              
      179          )
      180          async with self._lock:
      181              self._entries[trace_id] = entry

⏺ 接下来在 _run 的 finally 里加 finalizer 调用，并实现 _run_finalizer。

⏺ Update(core/tasks/registry.py)
  ⎿  Added 5 lines, removed 1 line
      325              # 发 done 之前先等 trace_runs 的 run_end 被 commit
            到 DB，
      326              # 避免前端收到 done 后立刻查快照落到其他 worker，
      327              # 后者回落 DB 时 trace_runs 仍是 running（docs/iss
           ue SSE 与快照不一致问题）。
      328 -            # 失败只降级 WARNING，仍照常 publish_done——不能因 
          -为 DB 抖动把 SSE 吊死。                                       
      328 +            # 失败只降级 WARNING,仍照常 publish_done——不能因为
          + DB 抖动把 SSE 吊死。                                         
      329              await self._await_trace_run_flushed(entry.trace_id
           )
      330 +            # 业务收尾（chat_messages 等）必须在 publish_done 
          +之前执行:                                                     
      331 +            # 前端收到 SSE done 后会立刻拉 sessions 判断消息终
          +态,这条顺序保证                                               
      332 +            # done 到达时 chat_messages 已经落到 success / err
          +or(Issue 3 修复核心)。                                        
      333 +            await self._run_finalizer(entry)                  
      334              self._publish_done(entry)
      335              self._bus.close(entry.trace_id)
      336              # 恢复 ContextVar，避免协程复用线程时污染后续任务
           。

⏺ 在 _await_trace_run_flushed 方法下面加 _run_finalizer 方法。

⏺ Update(core/tasks/registry.py)
  ⎿  Added 29 lines
      376                  trace_id,
      377              )
      378  
      379 +    async def _run_finalizer(self, entry: TaskEntry) -> None: 
      380 +        """在 publish_done 前执行业务收尾回调（如 chat_message
          +s 终态写入）。                                                
      381 +                                                              
      382 +        - 幂等：通过 ``entry.finalized`` 标志防止 _run.finally
          + 与 sweep 两条                                                
      383 +          路径重复调度时真的跑两次（首先进入的 coroutine 抢到 
          +标志位，后到的                                                
      384 +          短路返回）                                          
      385 +        - 隔离：finalizer 自身异常只记 WARNING，不阻塞 publish
          +_done；SSE 前                                                 
      386 +          端的终态可见性优先级高于 chat_messages 的一致性     
      387 +        - 超时：5 秒硬超时，避免 DB 死锁把整个任务吊死        
      388 +        """                                                   
      389 +        if entry.finalizer is None or entry.finalized:        
      390 +            return                                            
      391 +        entry.finalized = True                                
      392 +        try:                                                  
      393 +            await asyncio.wait_for(entry.finalizer(entry), tim
          +eout=5.0)                                                     
      394 +        except asyncio.TimeoutError:                          
      395 +            _logger.warning(                                  
      396 +                "finalizer timed out on trace %s after 5.0s; "
      397 +                "chat_messages 可能仍停在 pending,将靠启动期 r
          +ecover 或后续 sweep 收敛",                                    
      398 +                entry.trace_id,                               
      399 +            )                                                 
      400 +        except Exception:  # noqa: BLE001                     
      401 +            _logger.warning(                                  
      402 +                "finalizer failed on trace %s; "              
      403 +                "chat_messages 可能仍停在 pending",           
      404 +                entry.trace_id,                               
      405 +                exc_info=True,                                
      406 +            )                                                 
      407 +                                                              
      408      def _publish_status(self, entry: TaskEntry, state: TaskSta
           te) -> None:
      409          seq = self._bus.next_seq(entry.trace_id)
      410          self._bus.publish(

⏺ 在 _force_stalled 里也要触发 finalizer。

⏺ Update(core/tasks/registry.py)
  ⎿  Added 15 lines
      470              entry.duration_ms = (
      471                  entry.finished_at - entry.started_at
      472              ).total_seconds() * 1000.0
      473 +        # 僵尸纠偏路径也要触发 finalizer,否则 chat_messages 会
          +永远停在 pending                                              
      474 +        # (_run.finally 在 CancelledError 后虽然也会走 finaliz
          +er,但那时                                                     
      475 +        # entry.state 可能已被 _run 的 except 分支覆盖,此处先 
          +占位更可靠)。                                                 
      476 +        if entry.finalizer is not None and not entry.finalized
          +:                                                             
      477 +            try:                                              
      478 +                asyncio.create_task(                          
      479 +                    self._run_finalizer(entry),               
      480 +                    name=f"stalled-finalize:{entry.trace_id}",
      481 +                )                                             
      482 +            except Exception:  # noqa: BLE001                 
      483 +                _logger.warning(                              
      484 +                    "failed to schedule finalizer during stall
          + sweep: trace=%s",                                            
      485 +                    entry.trace_id,                           
      486 +                    exc_info=True,                            
      487 +                )                                             
      488          try:
      489              self._publish_done(entry)
      490          except Exception:  # noqa: BLE001

⏺ 现在改 api/routes/analyze_async.py：拆出 _build_finalizer，精简 runner。

⏺ Update(api/routes/analyze_async.py)
  ⎿  Added 6 lines, removed 1 line
      135  
      136      runner = _build_runner(
      137          request=request,
      138 -        chat_repo=chat_repo,                                  
      138          assistant_message_id=assistant_message_id,
      139          user_message_id=user_message_id,
      140      )
      141 +    finalizer = _build_finalizer(                             
      142 +        request=request,                                      
      143 +        chat_repo=chat_repo,                                  
      144 +        assistant_message_id=assistant_message_id,            
      145 +    )                                                         
      146  
      147      entry = await registry.submit(
      148          request,
     ...
      150          assistant_message_id=assistant_message_id,
      151          user_message_id=user_message_id,
      152          runner_factory=runner,
      153 +        finalizer=finalizer,                                  
      154      )
      155  
      156      # 使用 FastAPI 的 url_path_for 自动拼接 router 挂载 prefix

⏺ Update(api/routes/analyze_async.py)
  ⎿  Added 69 lines, removed 32 lines
      330  def _build_runner(
      331      *,
      332      request: AnalysisRequest,
      333 -    chat_repo: Any,                                           
      333      assistant_message_id: str | None,
      334      user_message_id: str | None,
      335  ):
      337 -    """返回一个闭包，供 TaskRegistry 作为 runner_factory 调用 
          -。"""                                                         
      336 +    """返回一个闭包，供 TaskRegistry 作为 runner_factory 调用 
          +。                                                            
      337  
      338 +    只做 orchestrator.analyze + 给 result 附 chat message id；
          +chat_messages                                                 
      339 +    的终态写入由 ``_build_finalizer`` 返回的 finalizer 在 regi
          +stry 的 finally                                               
      340 +    链路里统一执行，覆盖 timeout / cancel / stall-sweep 所有异
          +常路径。                                                      
      341 +    """                                                       
      342 +                                                              
      343      orchestrator = _get_orchestrator()
      344  
      345      async def runner(entry: TaskEntry) -> AnalysisResult:
      346          result = await orchestrator.analyze(request, trace_id=
           entry.trace_id)
      343 -                                                              
      344 -        # 统一给 result 附上 chat message id，便于前端直接消费
      347          if assistant_message_id is not None:
      348              result.assistant_message_id = assistant_message_id
      349          if user_message_id is not None:
      350              result.user_message_id = user_message_id
      351 +        return result                                         
      352  
      350 -        # 更新 assistant 消息：pending → success / error      
      351 -        if chat_repo is not None and assistant_message_id is n
          -ot None:                                                      
      352 -            content = result.report_markdown or (             
      353 -                result.error.message if result.error else "(无
          -内容)"                                                        
      353 +    return runner                                             
      354 +                                                              
      355 +                                                              
      356 +def _build_finalizer(                                         
      357 +    *,                                                        
      358 +    request: AnalysisRequest,                                 
      359 +    chat_repo: Any,                                           
      360 +    assistant_message_id: str | None,                         
      361 +):                                                            
      362 +    """返回 TaskRegistry 的 finalizer 回调：把 pending 气泡落 
          +成 success/error。                                            
      363 +                                                              
      364 +    关键点：finalizer 读 ``entry.state/result/error``（registr
          +y 在进 finally                                                
      365 +    前已赋值完毕），不依赖 runner 是否正常返回。于是 orchestra
          +tor 抛异常、                                                  
      366 +    runner 硬超时、CancelledError、stall-sweep 纠偏等所有终止 
          +路径都能把                                                    
      367 +    chat_messages 更新到正确终态——这是修复 Issue 3 的核心。   
      368 +                                                              
      369 +    None 表示不需要收尾（auto_persist=False 或没有 assistant  
          +消息）。                                                      
      370 +    """                                                       
      371 +    if chat_repo is None or assistant_message_id is None:     
      372 +        return None                                           
      373 +                                                              
      374 +    async def finalize(entry: TaskEntry) -> None:             
      375 +        if entry.state == TaskState.OK:                       
      376 +            content = (                                       
      377 +                entry.result.report_markdown                  
      378 +                if entry.result and entry.result.report_markdo
          +wn                                                            
      379 +                else "(无内容)"                               
      380              )
      355 -            msg_status = (                                    
      356 -                "error" if result.status == AnalysisStatus.FAI
          -LED else "success"                                            
      381 +            msg_status = "success"                            
      382 +        else:                                                 
      383 +            # 失败/中止/超时/僵尸：依次从 entry.error、entry.r
          +esult.error 里找消息                                          
      384 +            err_msg: str | None = None                        
      385 +            if entry.error is not None and entry.error.message
          +:                                                             
      386 +                err_msg = entry.error.message                 
      387 +            elif (                                            
      388 +                entry.result is not None                      
      389 +                and entry.result.error is not None            
      390 +                and entry.result.error.message                
      391 +            ):                                                
      392 +                err_msg = entry.result.error.message          
      393 +            content = err_msg or "分析失败"                   
      394 +            msg_status = "error"                              
      395 +                                                              
      396 +        duration_ms = int(entry.duration_ms or 0)             
      397 +        try:                                                  
      398 +            await _run_db_io(                                 
      399 +                chat_repo.update_message,                     
      400 +                request.user_id,                              
      401 +                request.session_id,                           
      402 +                assistant_message_id,                         
      403 +                {                                             
      404 +                    "content": content,                       
      405 +                    "status": msg_status,                     
      406 +                    "duration_ms": duration_ms,               
      407 +                    "trace_id": entry.trace_id,               
      408 +                },                                            
      409              )
      358 -            try:                                              
      359 -                await _run_db_io(                             
      360 -                    chat_repo.update_message,                 
      361 -                    request.user_id,                          
      362 -                    request.session_id,                       
      363 -                    assistant_message_id,                     
      364 -                    {                                         
      365 -                        "content": content,                   
      366 -                        "status": msg_status,                 
      367 -                        "duration_ms": int(result.duration_ms 
          -or 0),                                                        
      368 -                        "trace_id": result.trace_id or entry.t
          -race_id,                                                      
      369 -                    },                                        
      370 -                )                                             
      371 -            except Exception as exc:  # noqa: BLE001          
      372 -                _logger.warning(                              
      373 -                    "failed to finalize assistant message %s: 
          -%s",                                                          
      374 -                    assistant_message_id,                     
      375 -                    exc,                                      
      376 -                )                                             
      410 +        except Exception as exc:  # noqa: BLE001              
      411 +            # 让 registry._run_finalizer 感知失败（它会打 WARN
          +ING 并置 finalized                                            
      412 +            # 避免重试）；不抛 HTTPException 污染 registry 的 
          +日志堆栈。                                                    
      413 +            raise RuntimeError(                               
      414 +                f"update_message failed: msg_id={assistant_mes
          +sage_id} err={exc!r}"                                         
      415 +            ) from exc                                        
      416  
      378 -        return result                                         
      417 +    return finalize                                           
      418  
      380 -    return runner                                             
      419  
      382 -                                                              
      420  def _entry_to_snapshot(entry: TaskEntry) -> AnalysisTaskSnapsh
           ot:
      421      return AnalysisTaskSnapshot(
      422          trace_id=entry.trace_id,

  Searched for 2 patterns (ctrl+o to expand)

⏺ Update(api/routes/analyze_async.py)
  ⎿  Added 1 line, removed 1 line
      17  from fastapi.responses import StreamingResponse
      18  
      19  from api.routes.analyze import _get_orchestrator, _run_db_io
      20 -from api.schemas.analysis import AnalysisRequest, AnalysisResul
         -t, AnalysisStatus                                              
      20 +from api.schemas.analysis import AnalysisRequest, AnalysisResul
         +t                                                              
      21  from config.settings import get_settings
      22  from core.chat import get_chat_repository
      23  from core.logging_utils import get_logger

⏺ 修复 3 主体完成。先跑一下相关测试验证。

⏺ Bash(python -m pytest tests/unit/test_task_registry.py
      tests/unit/test_analyze_async_routes.py -x -q 2>&1 | tail -60)  ⎿  api/routes/analyze.py                         105     74    30%   40-4
     2, 58-60, 82-149, 156-158, 167-219, 238-246, 265-269, 289-291, 316-339
     api/routes/analyze_async.py                   227     42    81%   44-4
     … +88 lines (ctrl+o to expand)

⏺ 所有 32 个相关测试通过。现在加测试验证 finalizer 覆盖 timeout/cancel/stall
  全路径。

  Read 1 file (ctrl+o to expand)

⏺ Update(tests/unit/test_task_registry.py)
  ⎿  Added 227 lines
      415      # 放行 runner 让 entry.task 正常收尾，避免影响其它用例
      416      done_event.set()
      417      await asyncio.gather(entry.task, return_exceptions=True)  
           # type: ignore[arg-type]
      418 +                                                              
      419 +                                                              
      420 +# ------------------------------------------------------------
          +---------------                                               
      421 +# Finalizer 收尾：所有终止路径（成功/业务失败/运行时异常/硬超 
          +时/cancel/stall-sweep）                                       
      422 +# 都必须触发一次 finalizer，否则依赖它落地的 chat_messages.sta
          +tus 会停在 pending。                                          
      423 +# ------------------------------------------------------------
          +---------------                                               
      424 +                                                              
      425 +                                                              
      426 +@pytest.mark.asyncio                                          
      427 +async def test_finalizer_called_on_success(registry) -> None: 
      428 +    calls: list[TaskState] = []                               
      429 +                                                              
      430 +    async def finalizer(entry: TaskEntry) -> None:            
      431 +        calls.append(entry.state)                             
      432 +                                                              
      433 +    async def runner(entry: TaskEntry) -> AnalysisResult:     
      434 +        return _ok_result(entry.trace_id)                     
      435 +                                                              
      436 +    entry = await registry.submit(                            
      437 +        _req(), trace_id="t-fin-ok",                          
      438 +        assistant_message_id="m1", user_message_id=None,      
      439 +        runner_factory=runner, finalizer=finalizer,           
      440 +    )                                                         
      441 +    await entry.task  # type: ignore[arg-type]                
      442 +    assert calls == [TaskState.OK]                            
      443 +    assert entry.finalized is True                            
      444 +                                                              
      445 +                                                              
      446 +@pytest.mark.asyncio                                          
      447 +async def test_finalizer_called_on_business_failure(registry) 
          +-> None:                                                      
      448 +    from api.schemas.analysis import ErrorInfo                
      449 +                                                              
      450 +    calls: list[tuple[TaskState, str | None]] = []            
      451 +                                                              
      452 +    async def finalizer(entry: TaskEntry) -> None:            
      453 +        err_code = entry.error.code if entry.error else None  
      454 +        calls.append((entry.state, err_code))                 
      455 +                                                              
      456 +    async def runner(entry: TaskEntry) -> AnalysisResult:     
      457 +        return AnalysisResult(                                
      458 +            report_id=entry.trace_id,                         
      459 +            trace_id=entry.trace_id,                          
      460 +            status=AnalysisStatus.FAILED,                     
      461 +            analysis_type=AnalysisType.COMPREHENSIVE,         
      462 +            query="q", user_id="u1", session_id="s1", time_ran
          +ge="",                                                        
      463 +            error=ErrorInfo(code="LLM_CONNECTION_ERROR", messa
          +ge="boom"),                                                   
      464 +        )                                                     
      465 +                                                              
      466 +    entry = await registry.submit(                            
      467 +        _req(), trace_id="t-fin-failed",                      
      468 +        assistant_message_id="m2", user_message_id=None,      
      469 +        runner_factory=runner, finalizer=finalizer,           
      470 +    )                                                         
      471 +    await entry.task  # type: ignore[arg-type]                
      472 +    assert calls == [(TaskState.ERROR, "LLM_CONNECTION_ERROR")
          +]                                                             
      473 +                                                              
      474 +                                                              
      475 +@pytest.mark.asyncio                                          
      476 +async def test_finalizer_called_on_runner_exception(registry) 
          +-> None:                                                      
      477 +    """orchestrator 抛异常时,runner 里的老代码收不到 result,旧
          +实现会让                                                      
      478 +    chat_messages 停在 pending;finalizer 放到 registry.finally
          + 后必须被调。"""                                              
      479 +    calls: list[TaskState] = []                               
      480 +                                                              
      481 +    async def finalizer(entry: TaskEntry) -> None:            
      482 +        calls.append(entry.state)                             
      483 +                                                              
      484 +    async def runner(_entry: TaskEntry) -> AnalysisResult:    
      485 +        raise RuntimeError("boom from orchestrator")          
      486 +                                                              
      487 +    entry = await registry.submit(                            
      488 +        _req(), trace_id="t-fin-raise",                       
      489 +        assistant_message_id="m3", user_message_id=None,      
      490 +        runner_factory=runner, finalizer=finalizer,           
      491 +    )                                                         
      492 +    await asyncio.gather(entry.task, return_exceptions=True)  
          +# type: ignore[arg-type]                                      
      493 +    assert calls == [TaskState.ERROR]                         
      494 +    assert entry.error is not None and "boom" in entry.error.m
          +essage                                                        
      495 +                                                              
      496 +                                                              
      497 +@pytest.mark.asyncio                                          
      498 +async def test_finalizer_called_on_cancel(registry) -> None:  
      499 +    started = asyncio.Event()                                 
      500 +    calls: list[TaskState] = []                               
      501 +                                                              
      502 +    async def finalizer(entry: TaskEntry) -> None:            
      503 +        calls.append(entry.state)                             
      504 +                                                              
      505 +    async def runner(_entry: TaskEntry) -> AnalysisResult:    
      506 +        started.set()                                         
      507 +        await asyncio.sleep(10)                               
      508 +        raise AssertionError                                  
      509 +                                                              
      510 +    entry = await registry.submit(                            
      511 +        _req(), trace_id="t-fin-cancel",                      
      512 +        assistant_message_id="m4", user_message_id=None,      
      513 +        runner_factory=runner, finalizer=finalizer,           
      514 +    )                                                         
      515 +    await started.wait()                                      
      516 +    assert entry.task is not None                             
      517 +    entry.task.cancel()                                       
      518 +    with pytest.raises(asyncio.CancelledError):               
      519 +        await entry.task                                      
      520 +    assert calls == [TaskState.ABORTED]                       
      521 +                                                              
      522 +                                                              
      523 +@pytest.mark.asyncio                                          
      524 +async def test_finalizer_called_on_runner_hard_timeout(       
      525 +    short_timeout_registry,                                   
      526 +) -> None:                                                    
      527 +    calls: list[tuple[TaskState, str | None]] = []            
      528 +                                                              
      529 +    async def finalizer(entry: TaskEntry) -> None:            
      530 +        err_code = entry.error.code if entry.error else None  
      531 +        calls.append((entry.state, err_code))                 
      532 +                                                              
      533 +    async def stuck(_entry: TaskEntry) -> AnalysisResult:     
      534 +        await asyncio.sleep(5.0)                              
      535 +        raise AssertionError                                  
      536 +                                                              
      537 +    entry = await short_timeout_registry.submit(              
      538 +        _req(), trace_id="t-fin-timeout",                     
      539 +        assistant_message_id="m5", user_message_id=None,      
      540 +        runner_factory=stuck, finalizer=finalizer,            
      541 +    )                                                         
      542 +    await asyncio.gather(entry.task, return_exceptions=True)  
          +# type: ignore[arg-type]                                      
      543 +    assert calls == [(TaskState.ERROR, "RUNNER_STALLED")]     
      544 +                                                              
      545 +                                                              
      546 +@pytest.mark.asyncio                                          
      547 +async def test_finalizer_called_on_stall_sweep(short_timeout_r
          +egistry) -> None:                                             
      548 +    """sweep 强制纠偏僵尸 RUNNING entry 时也必须触发 finalizer
          +,                                                             
      549 +    否则 chat_messages 永远停在 pending(僵尸任务是 P0 场景)。"
          +""                                                            
      550 +    started = asyncio.Event()                                 
      551 +    calls: list[TaskState] = []                               
      552 +                                                              
      553 +    async def finalizer(entry: TaskEntry) -> None:            
      554 +        calls.append(entry.state)                             
      555 +                                                              
      556 +    async def hang(_entry: TaskEntry) -> AnalysisResult:      
      557 +        started.set()                                         
      558 +        await asyncio.sleep(30)                               
      559 +        raise AssertionError                                  
      560 +                                                              
      561 +    entry = await short_timeout_registry.submit(              
      562 +        _req(), trace_id="t-fin-sweep",                       
      563 +        assistant_message_id="m6", user_message_id=None,      
      564 +        runner_factory=hang, finalizer=finalizer,             
      565 +    )                                                         
      566 +    await started.wait()                                      
      567 +    # 模拟 wait_for 失灵:强行把 entry 拉回 RUNNING + 超龄     
      568 +    entry.started_at = now_cn() - timedelta(seconds=10)       
      569 +    entry.state = TaskState.RUNNING                           
      570 +    entry.error = None                                        
      571 +    entry.finished_at = None                                  
      572 +                                                              
      573 +    short_timeout_registry._sweep_once()                      
      574 +                                                              
      575 +    # sweep 通过 asyncio.create_task 调度 finalizer,给调度器一
          +轮机会                                                        
      576 +    for _ in range(10):                                       
      577 +        if calls:                                             
      578 +            break                                             
      579 +        await asyncio.sleep(0.01)                             
      580 +    assert calls == [TaskState.ERROR]                         
      581 +                                                              
      582 +    # 等 entry.task 自然收尾,防止影响其它用例                 
      583 +    await asyncio.gather(entry.task, return_exceptions=True)  
          +# type: ignore[arg-type]                                      
      584 +                                                              
      585 +                                                              
      586 +@pytest.mark.asyncio                                          
      587 +async def test_finalizer_exception_does_not_block_done(registr
          +y, bus) -> None:                                              
      588 +    """finalizer 自身失败只打 WARNING,不能阻塞 publish_done—SS
          +E 可见性优先。"""                                             
      589 +                                                              
      590 +    async def boom_finalizer(_entry: TaskEntry) -> None:      
      591 +        raise RuntimeError("finalizer boom")                  
      592 +                                                              
      593 +    async def runner(entry: TaskEntry) -> AnalysisResult:     
      594 +        return _ok_result(entry.trace_id)                     
      595 +                                                              
      596 +    entry = await registry.submit(                            
      597 +        _req(), trace_id="t-fin-boom",                        
      598 +        assistant_message_id="m7", user_message_id=None,      
      599 +        runner_factory=runner, finalizer=boom_finalizer,      
      600 +    )                                                         
      601 +    await entry.task  # type: ignore[arg-type]                
      602 +                                                              
      603 +    # done 仍然被发出;前端不受 finalizer 失败影响             
      604 +    dones = [e for e in bus.buffered("t-fin-boom") if e.get("t
          +ype") == "done"]                                              
      605 +    assert dones and dones[-1]["status"] == "ok"              
      606 +    # finalized 标志仍被置位,避免后续重试无限循环             
      607 +    assert entry.finalized is True                            
      608 +                                                              
      609 +                                                              
      610 +@pytest.mark.asyncio                                          
      611 +async def test_finalizer_timeout_degrades_gracefully(registry,
          + bus) -> None:                                                
      612 +    """finalizer 卡超过 5s 硬超时时降级 WARNING,不卡 done。"""
      613 +                                                              
      614 +    async def slow_finalizer(_entry: TaskEntry) -> None:      
      615 +        await asyncio.sleep(30)                               
      616 +                                                              
      617 +    async def runner(entry: TaskEntry) -> AnalysisResult:     
      618 +        return _ok_result(entry.trace_id)                     
      619 +                                                              
      620 +    entry = await registry.submit(                            
      621 +        _req(), trace_id="t-fin-slow",                        
      622 +        assistant_message_id="m8", user_message_id=None,      
      623 +        runner_factory=runner, finalizer=slow_finalizer,      
      624 +    )                                                         
      625 +    # 把 registry 的 finalizer 超时从 5s 改到 0.1s 以加速测试 
      626 +    original = registry._run_finalizer                        
      627 +                                                              
      628 +    async def _patched(entry: TaskEntry) -> None:             
      629 +        if entry.finalizer is None or entry.finalized:        
      630 +            return                                            
      631 +        entry.finalized = True                                
      632 +        try:                                                  
      633 +            await asyncio.wait_for(entry.finalizer(entry), tim
          +eout=0.1)                                                     
      634 +        except asyncio.TimeoutError:                          
      635 +            pass                                              
      636 +                                                              
      637 +    registry._run_finalizer = _patched  # type: ignore[method-
          +assign]                                                       
      638 +    try:                                                      
      639 +        await asyncio.wait_for(entry.task, timeout=2.0)  # typ
          +e: ignore[arg-type]                                           
      640 +    finally:                                                  
      641 +        registry._run_finalizer = original  # type: ignore[met
          +hod-assign]                                                   
      642 +                                                              
      643 +    dones = [e for e in bus.buffered("t-fin-slow") if e.get("t
          +ype") == "done"]                                              
      644 +    assert dones and dones[-1]["status"] == "ok"              

⏺ Bash(python -m pytest tests/unit/test_task_registry.py -x -q --no-cov 2>&1 |
      tail -20)
  ⎿  .....................
        [100%]
     21 passed in 2.89s

⏺ 全部通过。标记修复 3 完成，切到修复 3 后续（周期性 sweep）。

⏺ Update(core/tasks/registry.py)
  ⎿  Added 5 lines
      81          trace_flush_barrier_timeout: float = 2.0,
      82          runner_hard_timeout_seconds: float = 600.0,
      83          runner_stall_grace_seconds: float = 60.0,
      84 +        orphan_pending_chat_max_age_sec: int = 1800,           
      85      ) -> None:
      86          self._bus = event_bus
      87          self._session_factory = session_factory
     ...
       95          # （覆盖 wait_for 自身失灵或被屏蔽的边缘场景）。
       96          self._runner_hard_timeout = runner_hard_timeout_second
           s
       97          self._runner_stall_grace = runner_stall_grace_seconds
       98 +        # 孤儿 pending chat_messages 纠偏阈值：finalizer 是第 
          +一道闸，                                                      
       99 +        # _mark_stale_as_aborted(启动/关闭) 是第二道闸，本周期
          +性 sweep 是第三道闸，                                         
      100 +        # 覆盖"进程存活但 finalizer 全链路失败"的极端尾部场景 
          +。默认 30 分钟。                                              
      101 +        self._orphan_pending_chat_max_age = orphan_pending_cha
          +t_max_age_sec                                                 
      102          self._stalled_count = 0
      103          self._max_concurrent_tasks = max_concurrent_tasks
      104          self._semaphore = asyncio.Semaphore(max_concurrent_tas
           ks)

⏺ Update(core/tasks/registry.py)
  ⎿  Added 14 lines
      558              self._bus.drop(trace_id)
      559          if to_drop:
      560              _logger.info("task registry swept %d expired entri
           es", len(to_drop))
      561 +        # 孤儿 pending 消息兜底：finalizer + 启动 recover 都失
          +效的极端场景                                                  
      562 +        # （进程 OOM/Kill 在 POST 之后、finalizer 未跑之前就挂
          +掉且立刻重启                                                  
      563 +        # 也不会触发 _mark_stale_as_aborted；或 finalizer 连续
          + 5s 超时）。                                                  
      564 +        try:                                                  
      565 +            affected = self._sweep_orphan_pending_chat_message
          +s()                                                           
      566 +            if affected:                                      
      567 +                _logger.warning(                              
      568 +                    "task registry sweep marked %d orphan pend
          +ing chat_messages as error "                                  
      569 +                    "(age>%ds); 这批消息对应的 finalizer 链路 
          +全部失败",                                                    
      570 +                    affected,                                 
      571 +                    self._orphan_pending_chat_max_age,        
      572 +                )                                             
      573 +        except Exception:  # noqa: BLE001                     
      574 +            _logger.exception("orphan pending chat_messages sw
          +eep failed")                                                  
      575  
      576      # --------------------------------------------------------
           ----------
      577      # 启动兜底：把跨进程残留状态收敛

⏺ Update(core/tasks/registry.py)
  ⎿  Added 32 lines
      577      # 启动兜底：把跨进程残留状态收敛
      578      # --------------------------------------------------------
           ----------
      579  
      580 +    def _sweep_orphan_pending_chat_messages(self) -> int:     
      581 +        """把超龄且仍处于 pending 的 chat_messages 推到 error 
          +。                                                            
      582 +                                                              
      583 +        该 sweep 是 finalizer 的最后一道兜底：                
      584 +        1. registry._run_finalizer 是第一道闸（覆盖绝大多数终 
          +态路径）                                                      
      585 +        2. _mark_stale_as_aborted 是第二道闸（仅在启动/关闭触 
          +发）                                                          
      586 +        3. 本方法是第三道闸（进程存活期间的周期性清理）       
      587 +                                                              
      588 +        阈值由 ``orphan_pending_chat_max_age_sec`` 控制，默认 
          +1800 秒。                                                     
      589 +        必须足够大以避开正常业务最坏耗时（runner_hard_timeout 
          +600 + 余量），                                                
      590 +        太小会误杀仍在正常运行的任务。                        
      591 +        """                                                   
      592 +        from datetime import timedelta                        
      593 +                                                              
      594 +        threshold = now_cn() - timedelta(                     
      595 +            seconds=self._orphan_pending_chat_max_age         
      596 +        )                                                     
      597 +        with self._session_factory() as session:              
      598 +            affected = session.execute(                       
      599 +                update(chat_messages_table)                   
      600 +                .where(                                       
      601 +                    chat_messages_table.c.status == "pending",
      602 +                    chat_messages_table.c.created_at < thresho
          +ld,                                                           
      603 +                )                                             
      604 +                .values(                                      
      605 +                    status="error",                           
      606 +                    content="任务在后端无响应中终结,请重新发起
          +",                                                            
      607 +                )                                             
      608 +            ).rowcount or 0                                   
      609 +            session.commit()                                  
      610 +        return int(affected)                                  
      611 +                                                              
      612      def _mark_stale_as_aborted(self) -> dict[str, int]:
      613          """同步方法：跨进程启动/关闭时调用。"""
      614          affected_runs = 0

⏺ 现在把新参数串到 init_task_registry 和 settings / main。

⏺ Update(core/tasks/registry.py)
  ⎿  Added 2 lines
      660      trace_flush_barrier_timeout: float = 2.0,
      661      runner_hard_timeout_seconds: float = 600.0,
      662      runner_stall_grace_seconds: float = 60.0,
      663 +    orphan_pending_chat_max_age_sec: int = 1800,              
      664  ) -> TaskRegistry:
      665      """初始化全局 TaskRegistry（幂等）。"""
      666      global _registry
     ...
      674              trace_flush_barrier_timeout=trace_flush_barrier_ti
           meout,
      675              runner_hard_timeout_seconds=runner_hard_timeout_se
           conds,
      676              runner_stall_grace_seconds=runner_stall_grace_seco
           nds,
      677 +            orphan_pending_chat_max_age_sec=orphan_pending_cha
          +t_max_age_sec,                                                
      678          )
      679      return _registry
      680  

⏺ Update(config/settings.py)
  ⎿  Added 10 lines, removed 1 line
      348      # registry._run 在 publish_done 之前等 trace_runs 落库的最
           长阻塞秒数。
      349      # 保证前端收到 SSE done 时再查 /tasks/{id} 一定能看到终态
           。
      350      # 正常 DB 写入 10-50ms；超时会降级 WARNING 不阻塞 SSE。
      351 -    trace_flush_barrier_timeout: float = 2.0                  
      351 +    # 2026-04 生产复盘：2s 在 PG 抖动或大批量 trace span flush
          + 时偶尔不够,                                                  
      352 +    # 放宽到 10s;正常路径仍是 10-50ms,不影响吞吐。            
      353 +    trace_flush_barrier_timeout: float = 10.0                 
      354  
      355 +    # 周期性 sweep 扫"超龄仍 pending 的 chat_messages"的年龄阈
          +值（秒）。                                                    
      356 +    # registry.finalizer 是第一道闸（覆盖绝大多数路径）；     
      357 +    # _mark_stale_as_aborted 在启动/关闭时触发，是第二道闸；  
      358 +    # 本阈值用于进程存活期间的周期性第三道闸，默认 30 分钟。  
      359 +    # 值必须大于 runner_hard_timeout_seconds + 余量，否则会误 
          +杀还在跑的任务。                                              
      360 +    orphan_pending_chat_max_age_sec: int = 1800               
      361 +                                                              
      362      # registry._run 里 runner_factory 协程的硬超时秒数（覆盖 o
           rchestrator + 持久化 + chat 收尾）。
      363      # 触达此超时即 cancel runner 协程，把 entry 标为 ERROR(RUN
           NER_STALLED)，
      364      # 防止内存 entry.state 永远停在 RUNNING 导致 poll 一直返回
            running（见

⏺ Update(api/main.py)
  ⎿  Added 1 line
      175          trace_flush_barrier_timeout=async_cfg.trace_flush_barr
           ier_timeout,
      176          runner_hard_timeout_seconds=async_cfg.runner_hard_time
           out_seconds,
      177          runner_stall_grace_seconds=async_cfg.runner_stall_grac
           e_seconds,
      178 +        orphan_pending_chat_max_age_sec=async_cfg.orphan_pendi
          +ng_chat_max_age_sec,                                          
      179      )
      180      # 跨进程残留收敛：把上次进程中残留的 running / pending 标
           记为 aborted / error
      181      registry.recover_on_startup()

⏺ 同步修改 config.yaml 让新参数明面可见（保持 CLAUDE.md "配置项齐全" 要求）。

⏺ Update(config/config.yaml)
  ⎿  Added 2 lines
      135    sse_heartbeat_sec: 15               # SSE 心跳周期（秒），防
           代理 idle 超时
      136    runner_hard_timeout_seconds: 600.0  # runner_factory 协程硬
           超时，防止 entry.state 卡 RUNNING
      137    runner_stall_grace_seconds: 60.0    # sweep 纠偏僵尸 RUNNING
            entry 的宽限窗口
      138 +  trace_flush_barrier_timeout: 10.0   # publish_done 前等 trac
          +e_runs commit 的秒数                                          
      139 +  orphan_pending_chat_max_age_sec: 1800  # sweep 纠偏超龄 pend
          +ing chat_messages 的阈值                                      
      140  
      141    # SSE 事件总线后端。
      142    # - memory：进程内 asyncio.Queue，仅适用于 workers=1 的单进
           程部署

⏺ 加测试。

⏺ Update(tests/unit/test_task_registry.py)
  ⎿  Added 47 lines
      642  
      643      dones = [e for e in bus.buffered("t-fin-slow") if e.get("t
           ype") == "done"]
      644      assert dones and dones[-1]["status"] == "ok"
      645 +                                                              
      646 +                                                              
      647 +@pytest.mark.asyncio                                          
      648 +async def test_sweep_orphan_pending_chat_messages(bus, db_sess
          +ion_factory) -> None:                                         
      649 +    """模拟 finalizer 全链路失败留下的超龄 pending 消息,sweep 
          +必须把它们推到 error。"""                                     
      650 +    # 用极短阈值的 registry,方便测试                          
      651 +    short_orphan_registry = TaskRegistry(                     
      652 +        event_bus=bus,                                        
      653 +        session_factory=db_session_factory,                   
      654 +        max_concurrent_tasks=2,                               
      655 +        result_cache_ttl_sec=60,                              
      656 +        sweep_interval_sec=60,                                
      657 +        orphan_pending_chat_max_age_sec=1,  # 1 秒,几乎立即过 
          +期                                                            
      658 +    )                                                         
      659 +                                                              
      660 +    # 建一条 session 及两条消息:一条超龄 pending、一条新 pendi
          +ng                                                            
      661 +    with db_session_factory() as s:                           
      662 +        s.execute(chat_sessions_table.insert().values(        
      663 +            id="sess-orphan", user_id="u1", title="x",        
      664 +            title_auto=True, message_count=2,                 
      665 +        ))                                                    
      666 +        # 超龄 pending:created_at 往前推 10 秒                
      667 +        s.execute(chat_messages_table.insert().values(        
      668 +            id="m-old-pending", session_id="sess-orphan", role
          +="assistant",                                                 
      669 +            content="", status="pending", trace_id="trace-old"
          +,                                                             
      670 +            created_at=now_cn() - timedelta(seconds=10),      
      671 +        ))                                                    
      672 +        # 新 pending:不该被扫到                               
      673 +        s.execute(chat_messages_table.insert().values(        
      674 +            id="m-new-pending", session_id="sess-orphan", role
          +="assistant",                                                 
      675 +            content="", status="pending", trace_id="trace-new"
          +,                                                             
      676 +            created_at=now_cn(),                              
      677 +        ))                                                    
      678 +        s.commit()                                            
      679 +                                                              
      680 +    short_orphan_registry._sweep_once()                       
      681 +                                                              
      682 +    with db_session_factory() as s:                           
      683 +        old = s.execute(                                      
      684 +            select(chat_messages_table).where(chat_messages_ta
          +ble.c.id == "m-old-pending")                                  
      685 +        ).fetchone()                                          
      686 +        new = s.execute(                                      
      687 +            select(chat_messages_table).where(chat_messages_ta
          +ble.c.id == "m-new-pending")                                  
      688 +        ).fetchone()                                          
      689 +        assert old is not None and old.status == "error"      
      690 +        assert "重新发起" in old.content                      
      691 +        assert new is not None and new.status == "pending"    

⏺ Bash(python -m pytest tests/unit/test_task_registry.py -x -q --no-cov 2>&1 |
      tail -15)
  ⎿  ......................
        [100%]
     22 passed in 2.94s

⏺ 修复 1：memory backend + workers>1 fail-fast。

⏺ Update(api/main.py)
  ⎿  Added 13 lines, removed 9 lines
      54  
      55  
      56  def _check_event_backend_matches_workers(event_backend: str) ->
           None:
      57 -    """检查 event_backend 与实际 worker 数量是否匹配；不匹配打 
         -WARNING。                                                      
      57 +    """检查 event_backend 与实际 worker 数量是否匹配；不匹配直 
         +接 fail-fast。                                                 
      58  
      59      多 worker 部署下 memory backend 会导致 POST 与 SSE 可能落在
          不同 worker
      60 -    进程，订阅方永远收不到发布方的事件（详见                   
      61 -    docs/issue/async_analyze_backend_issue.md）。此处仅打 WARNI
         -NG，不阻止                                                     
      62 -    启动——运维可能临时调整 workers 数量做压测；但启动日志里能看
         -到明显提示。                                                   
      60 +    进程，订阅方永远收不到发布方的事件（前端 Issue 1 的根因，详
         +见                                                             
      61 +    docs/issue/async_analyze_backend_issue.md）。              
      62 +                                                               
      63 +    2026-04 生产复盘决定升级为 RuntimeError：WARNING 容易被忽略
         +，                                                             
      64 +    一旦用户点击"异步分析"就会表现为"界面转圈 15 分钟后失败"， 
      65 +    比服务启动不起来更糟糕。压测等临时场景请显式设置           
      66 +    ASYNC_ANALYSIS_EVENT_BACKEND=redis 再配合 workers>1 使用。 
      67      """
      68      import os
      69  
     ...
      73      except ValueError:
      74          workers = 1
      75      if workers > 1 and event_backend == "memory":
      72 -        _logger.warning(                                       
      73 -            "async_analysis.event_backend=memory 但检测到 worke
         -rs=%d；"                                                       
      74 -            "多 worker 下 SSE 事件无法跨进程送达，请改用 event_
         -backend=redis。"                                               
      75 -            "详见 docs/issue/async_analyze_backend_issue.md",  
      76 -            workers,                                           
      76 +        raise RuntimeError(                                    
      77 +            f"async_analysis.event_backend=memory 与 workers={w
         +orkers} 不兼容: "                                              
      78 +            "多 worker 下 SSE 事件无法跨进程送达,前端将只能收到
         +心跳事件。"                                                    
      79 +            "请在 config.yaml 设置 async_analysis.event_backend
         +=redis 并提供 redis_url,"                                      
      80 +            "或把 workers 降回 1。详见 docs/issue/async_analyze
         +_backend_issue.md"                                             
      81          )
      82  
      83  
  Searched for 2 patterns, read 1 file (ctrl+o to expand)

⏺ Update(tests/unit/test_event_bus.py)
  ⎿  Added 17 lines, removed 39 lines
      171      await task
      172  
      173  
      174 -def test_worker_mismatch_warning(monkeypatch) -> None:        
      175 -    """workers>1 且 backend=memory 时打 WARNING。"""          
      174 +def test_worker_mismatch_fail_fast(monkeypatch) -> None:      
      175 +    """workers>1 且 backend=memory 时必须 fail-fast 抛 Runtime
          +Error。                                                       
      176 +                                                              
      177 +    历史行为是 WARNING,但生产环境多次忽略该警告导致前端 Issue 
          +1                                                             
      178 +    复现(用户看到 15 分钟转圈后失败)。2026-04 复盘后改为直接  
      179 +    RuntimeError,逼迫运维在启动期修配置。                     
      180 +    """                                                       
      181      import api.main as api_main
      182  
      178 -    warnings: list[str] = []                                  
      179 -    monkeypatch.setattr(                                      
      180 -        api_main._logger,                                     
      181 -        "warning",                                            
      182 -        lambda msg, *a, **kw: warnings.append(msg % a if a els
          -e msg),                                                       
      183 -    )                                                         
      183      monkeypatch.setenv("WEB_CONCURRENCY", "4")
      185 -    api_main._check_event_backend_matches_workers("memory")   
      186 -    assert any("event_backend=memory" in w for w in warnings) 
      184 +    with pytest.raises(RuntimeError, match="event_backend=memo
          +ry"):                                                         
      185 +        api_main._check_event_backend_matches_workers("memory"
          +)                                                             
      186  
      187  
      189 -def test_worker_mismatch_no_warning_when_redis(monkeypatch) ->
          - None:                                                        
      190 -    """workers>1 且 backend=redis 时不打 WARNING。"""         
      188 +def test_worker_mismatch_ok_when_redis(monkeypatch) -> None:  
      189 +    """workers>1 且 backend=redis 时正常放行。"""             
      190      import api.main as api_main
      191  
      193 -    warnings: list[str] = []                                  
      194 -    monkeypatch.setattr(                                      
      195 -        api_main._logger,                                     
      196 -        "warning",                                            
      197 -        lambda msg, *a, **kw: warnings.append(msg % a if a els
          -e msg),                                                       
      198 -    )                                                         
      192      monkeypatch.setenv("WEB_CONCURRENCY", "4")
      200 -    api_main._check_event_backend_matches_workers("redis")    
      201 -    assert warnings == []                                     
      193 +    api_main._check_event_backend_matches_workers("redis")  # 
          +不抛                                                          
      194  
      195  
      204 -def test_worker_mismatch_no_warning_single_worker(monkeypatch)
          - -> None:                                                     
      205 -    """单 worker 下无论 backend 都不打 WARNING。"""           
      196 +def test_worker_mismatch_ok_single_worker(monkeypatch) -> None
          +:                                                             
      197 +    """单 worker 下无论 backend 都正常放行。"""               
      198      import api.main as api_main
      199  
      208 -    warnings: list[str] = []                                  
      209 -    monkeypatch.setattr(                                      
      210 -        api_main._logger,                                     
      211 -        "warning",                                            
      212 -        lambda msg, *a, **kw: warnings.append(msg % a if a els
          -e msg),                                                       
      213 -    )                                                         
      200      monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
      201      monkeypatch.delenv("WORKERS", raising=False)
      216 -    api_main._check_event_backend_matches_workers("memory")   
      217 -    assert warnings == []                                     
      202 +    api_main._check_event_backend_matches_workers("memory")  #
          + 不抛                                                         
      203  
      204  
      205  def test_worker_mismatch_invalid_env_treated_as_single(monkeyp
           atch) -> None:
      221 -    """WEB_CONCURRENCY 非法值应按单 worker 处理。"""          
      206 +    """WEB_CONCURRENCY 非法值应按单 worker 处理,不 fail-fast。
          +"""                                                           
      207      import api.main as api_main
      208  
      224 -    warnings: list[str] = []                                  
      225 -    monkeypatch.setattr(                                      
      226 -        api_main._logger,                                     
      227 -        "warning",                                            
      228 -        lambda msg, *a, **kw: warnings.append(msg % a if a els
          -e msg),                                                       
      229 -    )                                                         
      209      monkeypatch.setenv("WEB_CONCURRENCY", "not-a-number")
      231 -    api_main._check_event_backend_matches_workers("memory")   
      232 -    assert warnings == []                                     
      210 +    api_main._check_event_backend_matches_workers("memory")  #
          + 不抛                                                         
      211  
      212  
      213  def test_init_event_bus_memory_default() -> None:

⏺ Bash(python -m pytest tests/unit/test_event_bus.py
      tests/unit/test_task_registry.py tests/unit/test_analyze_async_routes.py
      -x -q --no-cov 2>&1 | tail -15)
  ⎿  ..........................................................
        [100%]
     58 passed in 6.25s

⏺ 全部通过。现在跑全量测试确认没回归。

⏺ Bash(python -m pytest --no-cov -q 2>&1 | tail -40)
  ⎿  ......................................................................
     .. [  6%]
     ......................................................................
     … +42 lines (ctrl+o to expand)

⏺ 1042 个用例全过，无回归。

⏺ 修复完成 —— 总览

  修复 3（主修）：chat_messages 终态一致性

  - core/tasks/registry.py：新增 Finalizer 类型与
  TaskEntry.finalizer/finalized；submit() 新增 finalizer 参数；_run() finally 在
   _await_trace_run_flushed 之后、_publish_done 之前调用
  _run_finalizer；_force_stalled 里 fire-and-forget 调度 finalizer
  - api/routes/analyze_async.py：把 chat 终态写入从 _build_runner 搬到新的
  _build_finalizer，注入 registry
  - 效果：orchestrator 抛异常 / runner 硬超时 / CancelledError / stall-sweep
  全部能把 chat_messages.status 落到 success/error，前端刷新不再循环重订阅 SSE

  修复 3 后续：周期性 sweep 兜底

  - core/tasks/registry.py：新增 _sweep_orphan_pending_chat_messages 在
  _sweep_once 调用；新增 orphan_pending_chat_max_age_sec 配置（默认 1800s）
  - config/settings.py + config/config.yaml：新增配置项

  修复 2：flush 屏障放宽

  - config/settings.py：trace_flush_barrier_timeout 默认 2.0 →
  10.0；config/config.yaml 同步

  修复 1：memory backend + workers>1 fail-fast

  - api/main.py：WARNING → RuntimeError，启动期直接拒绝

  测试

  - tests/unit/test_task_registry.py：新增 9 条 finalizer + sweep
  覆盖用例（success / business-failure / runner-exception / cancel /
  hard-timeout / stall-sweep / finalizer-exception / finalizer-timeout /
  orphan-sweep）
  - tests/unit/test_event_bus.py：把原 WARNING 断言改成
  pytest.raises(RuntimeError)
  - 全量 1042 用例通过，无回归