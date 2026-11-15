import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gideon import (
    AntigravityEngine,
    AudioStreamRecorder,
    BROWSER_DIRECTIVE,
    CODING_DIRECTIVE,
    CONVERSATION_DIRECTIVE,
    GIDEON_VOICE_PREAMBLE,
    TTSPlayer,
    _FishStream,
    _resolve_agy_model,
    classify_turn_directive,
)


class TestInterruption(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bus = MagicMock()
        self.tts = MagicMock(_is_speaking=False)
        self.tts.interrupt = AsyncMock()
        self.tts.wait_until_done = AsyncMock()
        self.engine = AntigravityEngine(self.bus, self.tts)
        self.engine.proc = MagicMock()
        self.engine.proc.returncode = None
        self.engine.proc.wait = AsyncMock(return_value=0)
        self.engine.proc.stdin.drain = AsyncMock()
        self.engine.proc.stdout = asyncio.StreamReader()

    async def asyncTearDown(self):
        await self.engine.close()

    async def test_interrupt_notifies_native_stdin_without_killing_process(self):
        await self.engine.send_prompt('Work on this')
        await self.engine.interrupt()
        payloads = [json.loads(c.args[0]) for c in self.engine.proc.stdin.write.call_args_list]
        self.assertEqual(len(payloads), 2)
        self.assertIn('Stop', payloads[-1]['message']['content'])
        self.engine.proc.terminate.assert_not_called()
        self.assertFalse(self.engine.is_turn_active)

    async def test_stalled_stdin_drain_does_not_block_next_input(self):
        blocked = asyncio.Event()
        self.engine.proc.stdin.drain.side_effect = blocked.wait
        await asyncio.wait_for(self.engine.send_prompt('Work'), timeout=.2)
        await asyncio.wait_for(self.engine.interrupt(), timeout=.2)
        self.assertFalse(self.engine.is_turn_active)

    async def test_old_result_cannot_finish_new_turn_or_speak_old_deltas(self):
        await self.engine.send_prompt('Old')
        await self.engine.interrupt()
        await self.engine.send_prompt('New')
        reader = asyncio.create_task(self.engine._read_loop())
        for event in [
            {'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'text_delta': 'Stale speech.'}},
            {'event': 'result'}, {'event': 'result'},
        ]:
            self.engine.proc.stdout.feed_data((json.dumps(event)+'\n').encode())
        await asyncio.sleep(0)
        self.assertTrue(self.engine.is_turn_active)
        self.tts.queue_sentence.assert_not_called()
        self.engine.proc.stdout.feed_data(b'{"event":"result"}\n')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(self.engine.is_turn_active)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    async def test_eof_clears_active_turn(self):
        await self.engine.send_prompt('Work')
        self.engine.proc.stdout.feed_eof()
        with patch.object(self.engine, 'start', new_callable=AsyncMock):
            await self.engine._read_loop()
        self.assertFalse(self.engine.is_turn_active)
        self.assertIsNone(self.engine.current_tool)

    async def test_old_tts_completion_does_not_rearm_hot_mic(self):
        finished = asyncio.Event()
        self.tts.wait_until_done.side_effect = finished.wait
        self.engine.wake_listener = MagicMock()
        await self.engine.send_prompt('Old')
        old_generation = self.engine._turn_generation
        completion = asyncio.create_task(self.engine._finish_turn(old_generation))
        await asyncio.sleep(0)
        await self.engine.send_prompt('New')
        finished.set()
        await completion
        self.assertTrue(self.engine.is_turn_active)
        self.engine.wake_listener.arm_hot_mic.assert_not_called()

    async def test_normal_result_finishes_and_rearms_45_second_hot_mic(self):
        self.engine.wake_listener = MagicMock()
        await self.engine.send_prompt('Hello')
        reader = asyncio.create_task(self.engine._read_loop())
        self.engine.proc.stdout.feed_data(b'{"event":"result"}\n')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(self.engine.is_turn_active)
        self.engine.wake_listener.arm_hot_mic.assert_called_once_with(timeout_s=45.0)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    async def test_broken_pipe_returns_idle_without_losing_input_loop(self):
        self.engine.proc.stdin.write.side_effect = BrokenPipeError
        await self.engine.send_prompt('Hello')
        self.assertFalse(self.engine.is_turn_active)
        self.bus.set_state.assert_called_with('idle', 'My engine connection failed. Please try that again.')
        self.tts.queue_sentence.assert_called_with('My engine connection failed. Please try that again.')

    async def test_reader_failure_clears_active_turn(self):
        await self.engine.send_prompt('Hello')
        self.engine.proc.stdout = MagicMock()
        self.engine.proc.stdout.readline = AsyncMock(side_effect=ConnectionResetError)
        with patch.object(self.engine, 'start', new_callable=AsyncMock):
            await self.engine._read_loop()
        self.assertFalse(self.engine.is_turn_active)

    def replacement_process(self):
        proc = MagicMock(returncode=None)
        proc.wait = AsyncMock(return_value=0)
        proc.stdin.drain = AsyncMock()
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(b'{"conversation_id":"saved-chat"}\n')
        return proc

    async def test_eof_warns_respawns_and_next_prompt_works(self):
        self.engine.conversation_id = 'saved-chat'
        await self.engine.send_prompt('Work')
        old = self.engine.proc
        old.returncode = 17
        old.stdout.feed_eof()
        replacement = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(return_value=replacement)) as spawn, \
                self.assertLogs('gideon', level='WARNING') as logs:
            await self.engine._read_loop()
        self.assertIn('17', '\n'.join(logs.output))
        self.assertIn('--conversation', spawn.call_args.args)
        self.assertIn('saved-chat', spawn.call_args.args)
        self.assertFalse(self.engine.is_turn_active)
        self.assertIsNone(self.engine._watchdog_task)
        self.assertEqual(list(self.engine._pending_turns), [])
        self.assertIsNotNone(self.engine._read_task)
        self.tts.queue_sentence.assert_called()
        await self.engine.send_prompt('Next')
        self.assertTrue(self.engine.is_turn_active)
        replacement.stdin.write.assert_called_once()
        self.assertEqual(list(self.engine._pending_turns), [self.engine._turn_generation])

    async def test_dead_proc_restarts_before_send_and_cancels_stale_drain(self):
        old = self.engine.proc
        old.returncode = 9
        stale_drain = asyncio.create_task(asyncio.Event().wait())
        self.engine._drain_task = stale_drain
        replacement = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(return_value=replacement)):
            await self.engine.send_prompt('Can you hear me?')
        old.stdin.write.assert_not_called()
        replacement.stdin.write.assert_called_once()
        self.assertTrue(stale_drain.cancelled())
        self.assertIsNot(self.engine._drain_task, stale_drain)
        self.assertTrue(self.engine.is_turn_active)

    async def test_interrupt_on_dead_engine_does_not_queue_orphan_stop(self):
        await self.engine.send_prompt('Old task')
        old = self.engine.proc
        old.returncode = 1
        replacement = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(return_value=replacement)):
            await self.engine.interrupt()
        replacement.stdin.write.assert_not_called()
        self.assertEqual(list(self.engine._pending_turns), [])
        await self.engine.send_prompt('New task')
        self.assertEqual(list(self.engine._pending_turns), [self.engine._turn_generation])

    async def test_write_guard_recovers_when_child_dies_after_send_alive_check(self):
        self.engine._is_first_turn = False
        self.engine._turn_generation = 4
        self.engine.is_turn_active = True
        old = self.engine.proc
        old.returncode = 1
        replacement = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(return_value=replacement)):
            self.assertTrue(await self.engine._write_prompt('New question', 4))
        old.stdin.write.assert_not_called()
        self.assertIn(GIDEON_VOICE_PREAMBLE, json.loads(replacement.stdin.write.call_args.args[0])['message']['content'])
        self.assertEqual(list(self.engine._pending_turns), [self.engine._turn_generation])
        self.assertTrue(self.engine.is_turn_active)

    async def test_resume_failure_starts_fresh_and_announces_context_loss_once(self):
        self.engine.conversation_id = 'saved-chat'
        self.engine._is_first_turn = False
        self.engine.proc.returncode = 1
        rejected = self.replacement_process()
        rejected.stdout = asyncio.StreamReader()
        rejected.stdout.feed_eof()
        rejected.returncode = 2
        replacement = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(side_effect=[rejected, replacement])) as spawn:
            await self.engine.send_prompt('Hello again')
        self.assertIn('--conversation', spawn.call_args_list[0].args)
        self.assertNotIn('--conversation', spawn.call_args_list[1].args)
        notices = [call.args[0] for call in self.tts.queue_sentence.call_args_list]
        self.assertEqual(sum('fresh conversation' in notice for notice in notices), 1)
        self.assertIn(GIDEON_VOICE_PREAMBLE, json.loads(replacement.stdin.write.call_args.args[0])['message']['content'])

    async def test_reconnect_failure_is_visible_and_next_prompt_retries(self):
        self.engine.proc.returncode = 1
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(side_effect=FileNotFoundError)):
            await self.engine.send_prompt('Hello')
        self.assertFalse(self.engine.is_turn_active)
        self.assertIn("couldn't reconnect", self.bus.current_reply)
        replacement = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(return_value=replacement)):
            await self.engine.send_prompt('Retry')
        replacement.stdin.write.assert_called_once()

    async def test_late_eof_from_old_reader_cannot_reset_replacement_turn(self):
        old = self.engine.proc
        reader = asyncio.create_task(self.engine._read_loop())
        await asyncio.sleep(0)
        self.engine.proc = self.replacement_process()
        await self.engine.send_prompt('New child')
        generation = self.engine._turn_generation
        with patch('gideon.asyncio.create_subprocess_exec', new_callable=AsyncMock) as spawn:
            old.stdout.feed_eof()
            await reader
        spawn.assert_not_called()
        self.assertTrue(self.engine.is_turn_active)
        self.assertEqual(list(self.engine._pending_turns), [generation])

    async def test_close_during_reconnect_cancels_replacement_reader(self):
        self.engine.proc.returncode = 1
        self.engine.proc.stdout.feed_eof()
        spawning = asyncio.Event()
        release = asyncio.Event()
        replacement = self.replacement_process()

        async def spawn(*args, **kwargs):
            spawning.set()
            await release.wait()
            return replacement

        with patch('gideon.asyncio.create_subprocess_exec', side_effect=spawn):
            self.engine._read_task = asyncio.create_task(self.engine._read_loop())
            recovery = self.engine._read_task
            await asyncio.wait_for(spawning.wait(), timeout=1)
            closing = asyncio.create_task(self.engine.close())
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(asyncio.gather(recovery, closing), timeout=1)
        self.assertTrue(self.engine._closed)
        self.assertIsNone(self.engine._read_task)
        self.assertFalse(self.engine.is_turn_active)

    async def test_recorder_stop_failure_still_closes_callback_stream(self):
        recorder = AudioStreamRecorder(self.bus)
        with patch('sounddevice.InputStream'):
            recorder.start()
            owner = recorder.stream
            owner.stop.side_effect = RuntimeError('stop failed')
            recorder.stop()
            owner.close.assert_called_once()
            self.assertIsNone(recorder.stream)

    async def test_audio_open_failure_never_globally_resets_other_streams(self):
        from audio.wake import AudioListener
        listener = AudioListener()
        listener.wakeword_model = MagicMock()
        with patch('sounddevice.InputStream', side_effect=RuntimeError('device unavailable')), \
                patch('sounddevice._terminate') as terminate, \
                patch('sounddevice._initialize') as initialize:
            with self.assertRaises(RuntimeError):
                await listener.listen_for_command(is_hot_mic=False, is_speaking=False)
            terminate.assert_not_called()
            initialize.assert_not_called()

    async def test_duplicate_recorder_start_keeps_callback_owner_alive(self):
        recorder = AudioStreamRecorder(self.bus)
        with patch('sounddevice.InputStream') as stream:
            recorder.start()
            owner = recorder.stream
            recorder.start()
            self.assertIs(recorder.stream, owner)
            stream.assert_called_once()
            recorder.stop()
            owner.close.assert_called_once()

    def test_observed_browser_guardrails(self):
        for phrase in ('observed tab', 'exact URL', 'verify', 'NEVER invent'):
            self.assertIn(phrase, GIDEON_VOICE_PREAMBLE)

    async def test_start_pins_flash_model_and_rejects_startup_error_result(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('GIDEON_AGY_MODEL', None)
            self.assertEqual(_resolve_agy_model('low'), 'gemini-3.8-flash-low')
            self.assertEqual(_resolve_agy_model('medium'), 'gemini-3.8-flash-medium')
            self.assertEqual(_resolve_agy_model('high'), 'gemini-3.8-flash-high')
            self.assertEqual(_resolve_agy_model('xhigh'), 'gemini-3.8-flash-high')
            self.assertEqual(_resolve_agy_model('max'), 'gemini-3.8-flash-high')

        replacement = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(return_value=replacement)) as spawn, \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop('GIDEON_AGY_MODEL', None)
            await self.engine.start(effort='low')
        args = spawn.call_args.args
        self.assertIn('--model', args)
        self.assertEqual(args[args.index('--model') + 1], 'gemini-3.8-flash-low')
        self.assertIn('--effort', args)
        self.assertEqual(args[args.index('--effort') + 1], 'low')

        bad = MagicMock(returncode=1)
        bad.wait = AsyncMock(return_value=1)
        bad.stdout = asyncio.StreamReader()
        bad.stdout.feed_data(
            b'{"event":"result","result":{"status":"ERROR","error":"bad model quota"}}\n'
        )
        bad.stdout.feed_eof()
        bad.stderr = asyncio.StreamReader()
        bad.stderr.feed_data(b'error: bad model quota\n')
        bad.stderr.feed_eof()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(return_value=bad)), \
                self.assertLogs('gideon', level='ERROR') as logs:
            with self.assertRaises(RuntimeError) as ctx:
                await self.engine.start(effort='low')
        self.assertIn('bad model quota', str(ctx.exception))
        self.assertIn('error: bad model quota', '\n'.join(logs.output))

    async def test_start_retries_without_effort_when_custom_model_rejects_effort(self):
        rejected = MagicMock(returncode=1)
        rejected.wait = AsyncMock(return_value=1)
        rejected.stdout = asyncio.StreamReader()
        rejected.stdout.feed_data(
            b'{"event":"result","result":{"status":"ERROR","error":"--effort is not supported for the current model"}}\n'
        )
        rejected.stdout.feed_eof()
        rejected.stderr = asyncio.StreamReader()
        rejected.stderr.feed_eof()

        accepted = self.replacement_process()
        with patch.dict(os.environ, {'GIDEON_AGY_MODEL': 'custom-model'}), \
                patch('gideon.asyncio.create_subprocess_exec', AsyncMock(side_effect=[rejected, accepted])) as spawn:
            await self.engine.start(effort='low')
        self.assertEqual(spawn.call_count, 2)
        self.assertIn('--effort', spawn.call_args_list[0].args)
        self.assertNotIn('--effort', spawn.call_args_list[1].args)
        self.assertEqual(spawn.call_args_list[1].args[spawn.call_args_list[1].args.index('--model') + 1], 'custom-model')

    async def test_account_change_restarts_idle_engine_and_clears_conversation(self):
        first = self.replacement_process()
        second = self.replacement_process()
        with patch('gideon._read_active_antigravity_account', side_effect=['first@example.com', 'other@gmail.com', 'other@gmail.com', 'other@gmail.com']), \
                patch('gideon.asyncio.create_subprocess_exec', AsyncMock(side_effect=[first, second])) as spawn:
            await self.engine.start(effort='low')
            self.assertEqual(self.engine.conversation_id, 'saved-chat')
            self.assertEqual(self.engine._auth_account, 'first@example.com')
            await self.engine.send_prompt('Hello')
        self.assertEqual(spawn.call_count, 2)
        self.assertNotIn('--conversation', spawn.call_args_list[1].args)
        self.assertEqual(self.engine._auth_account, 'other@gmail.com')
        first.terminate.assert_called_once()
        second.stdin.write.assert_called_once()

    async def test_rapid_post_init_crashes_pause_auto_reconnect_until_next_prompt(self):
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(side_effect=lambda *a, **k: self.replacement_process())), \
                patch.object(self.engine, '_reconnect_backoff', AsyncMock()):
            await self.engine.start(effort='low')
            for _ in range(2):
                proc = self.engine.proc
                proc.returncode = 1
                self.assertTrue(await self.engine._ensure_engine_alive(failed_proc=proc))
            crashed = self.engine.proc
            crashed.returncode = 1
            self.assertFalse(await self.engine._ensure_engine_alive(failed_proc=crashed))
        self.assertIn('keeps dropping', self.bus.current_reply)

        recovery = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(return_value=recovery)):
            await self.engine.send_prompt('Try again')
        recovery.stdin.write.assert_called_once()

    async def test_tts_player_returns_to_idle_and_suppresses_interrupt_error_log(self):
        bus = MagicMock(current_state='idle')

        def _set_state(state, detail=''):
            bus.current_state = state

        bus.set_state.side_effect = _set_state
        player = TTSPlayer(bus)

        ok_proc = MagicMock(returncode=0)
        ok_proc.poll.return_value = 0
        ok_proc.communicate.return_value = (b'', b'')
        with patch('gideon.subprocess.Popen', return_value=ok_proc):
            await player.play_wav(b'RIFFfake', text='Hello')
        self.assertEqual(bus.current_state, 'idle')

        killed_proc = MagicMock(returncode=-15)
        killed_proc.poll.return_value = None

        def _communicate(_wav):
            player._interrupted_procs.add(killed_proc)
            return (b'', b'Terminated')

        killed_proc.communicate.side_effect = _communicate
        with patch('gideon.subprocess.Popen', return_value=killed_proc), \
                patch('gideon.logger.error') as log_err:
            await player.play_wav(b'RIFFfake', text='Interrupted')
        log_err.assert_not_called()

    async def test_tts_player_preserves_toolcall_state_while_tool_is_running(self):
        bus = MagicMock(
            current_state='toolcall',
            current_tool={'id': 't_1', 'name': 'run_command', 'label': 'run_command: ls', 'status': 'running'},
        )
        states = []

        def _set_state(state, detail=''):
            bus.current_state = state
            states.append((state, detail))

        bus.set_state.side_effect = _set_state
        player = TTSPlayer(bus)

        ok_proc = MagicMock(returncode=0)
        ok_proc.poll.return_value = 0
        ok_proc.communicate.return_value = (b'', b'')
        with patch('gideon.subprocess.Popen', return_value=ok_proc):
            await player.play_wav(b'RIFFfake', text='On it')

        self.assertEqual(bus.current_state, 'toolcall')
        self.assertNotIn('speaking', [s for s, _ in states])
        self.assertNotIn('idle', [s for s, _ in states])
        self.assertEqual(states[-1], ('toolcall', 'run_command: ls'))

    async def test_tts_player_returns_to_conversing_while_turn_is_active(self):
        bus = MagicMock(current_state='toolcall', current_tool=None)
        bus.engine = MagicMock(is_turn_active=True)
        states = []

        def _set_state(state, detail=''):
            bus.current_state = state
            states.append((state, detail))

        bus.set_state.side_effect = _set_state
        player = TTSPlayer(bus)

        ok_proc = MagicMock(returncode=0)
        ok_proc.poll.return_value = 0
        ok_proc.communicate.return_value = (b'', b'')
        with patch('gideon.subprocess.Popen', return_value=ok_proc):
            await player.play_wav(b'RIFFfake', text='Still running')

        self.assertEqual(bus.current_state, 'conversing')
        self.assertNotIn('idle', [s for s, _ in states])

    async def test_dynamic_directive_injection_for_browser_and_coding_intents(self):
        import reflex_router

        browser_disp = MagicMock()
        browser_disp.predict.return_value = ('browser_use_passthrough', 0.99, None)
        self.assertEqual(classify_turn_directive('check my instagram dms', dispatcher=browser_disp), BROWSER_DIRECTIVE)

        coding_disp = MagicMock()
        coding_disp.predict.return_value = ('hard_goal', 0.99, None)
        self.assertEqual(classify_turn_directive('fix the bug in gideon.py', dispatcher=coding_disp), CODING_DIRECTIVE)

        conv_disp = MagicMock()
        conv_disp.predict.return_value = ('conversation', 0.99, None)
        self.assertEqual(classify_turn_directive('how are you doing', dispatcher=conv_disp), CONVERSATION_DIRECTIVE)

        with patch.object(reflex_router, '_gideon_dispatcher_instance', browser_disp):
            await self.engine.send_prompt('check my instagram dms')
            payload1 = json.loads(self.engine.proc.stdin.write.call_args_list[-1].args[0])
            self.assertIn(GIDEON_VOICE_PREAMBLE, payload1['message']['content'])
            self.assertIn(BROWSER_DIRECTIVE, payload1['message']['content'])

        with patch.object(reflex_router, '_gideon_dispatcher_instance', coding_disp):
            await self.engine.send_prompt('fix the bug in gideon.py')
            payload2 = json.loads(self.engine.proc.stdin.write.call_args_list[-1].args[0])
            self.assertNotIn(GIDEON_VOICE_PREAMBLE, payload2['message']['content'])
            self.assertIn(CODING_DIRECTIVE, payload2['message']['content'])

    def test_special_agent_workspace_reveal_reflex(self):
        import reflex_router

        for phrase in (
            "show me what you're doing",
            "show agent workspace",
            "toggle agent workspace",
            "hide agent workspace",
        ):
            parsed = reflex_router.parse_command(phrase)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed['intent'], 'system_action')
            self.assertEqual(parsed['argv'], ['hyprctl', 'dispatch', 'hl.dsp.workspace.toggle_special("agent")'])
            self.assertEqual(reflex_router._to_hypr_lua(parsed['argv']), 'hl.dsp.workspace.toggle_special("agent")')

    async def test_read_loop_tracks_agy_tool_active_and_done_events(self):
        self.engine.is_turn_active = True
        self.engine._pending_turns.append(self.engine._turn_generation)
        states = []
        self.bus.set_state = MagicMock(side_effect=lambda s, detail='': states.append((s, detail)))

        events = [
            json.dumps({
                'event': 'step_update',
                'step_update': {
                    'step_type': 'tool',
                    'state': 'ACTIVE',
                    'tool_name': 'run_command',
                    'tool_info': {'parameters': {'CommandLine': '~/.local/bin/music online "Hotel California"'}},
                },
            }).encode() + b'\n',
            json.dumps({
                'event': 'step_update',
                'step_update': {
                    'step_type': 'tool',
                    'state': 'DONE',
                    'tool_name': 'run_command',
                    'tool_info': {'parameters': {'CommandLine': '~/.local/bin/music online "Hotel California"'}},
                },
            }).encode() + b'\n',
            json.dumps({'event': 'result', 'result': {'status': 'OK'}}).encode() + b'\n',
            b'',
        ]

        stdout = MagicMock()
        stdout.readline = AsyncMock(side_effect=events)
        proc = MagicMock(returncode=0, stdout=stdout)
        self.engine.proc = proc

        with patch.object(self.engine, '_ensure_engine_alive', AsyncMock()):
            await self.engine._read_loop()

        self.assertTrue(any(s == 'toolcall' and 'music online' in d for s, d in states), states)
        self.assertEqual(self.bus.current_tool['status'], 'done')
        self.assertEqual(self.bus.current_tool['name'], 'run_command')

    async def test_read_loop_deduplicates_active_step_and_handles_tool_error(self):
        self.engine.is_turn_active = True
        self.engine._pending_turns.append(self.engine._turn_generation)
        states = []
        self.bus.set_state = MagicMock(side_effect=lambda s, detail='': states.append((s, detail)))

        events = [
            json.dumps({
                'event': 'step_update',
                'step_update': {
                    'step_index': 7,
                    'step_type': 'tool',
                    'state': 'ACTIVE',
                    'tool_name': 'run_command',
                    'tool_info': {'parameters': {'CommandLine': 'echo one'}},
                },
            }).encode() + b'\n',
            json.dumps({
                'event': 'step_update',
                'step_update': {
                    'step_index': 7,
                    'step_type': 'tool',
                    'state': 'ACTIVE',
                    'tool_name': 'run_command',
                    'tool_info': {'parameters': {'CommandLine': 'echo one'}},
                },
            }).encode() + b'\n',
            json.dumps({
                'event': 'step_update',
                'step_update': {
                    'step_index': 7,
                    'step_type': 'tool',
                    'state': 'ERROR',
                    'tool_name': 'run_command',
                },
            }).encode() + b'\n',
            json.dumps({'event': 'result', 'result': {'status': 'OK'}}).encode() + b'\n',
            b'',
        ]

        stdout = MagicMock()
        stdout.readline = AsyncMock(side_effect=events)
        self.engine.proc = MagicMock(returncode=0, stdout=stdout)

        with patch.object(self.engine, '_ensure_engine_alive', AsyncMock()):
            await self.engine._read_loop()

        tool_entries = [s for s in states if s[0] == 'toolcall']
        self.assertEqual(len(tool_entries), 1, states)
        self.assertEqual(self.bus.current_tool['id'], 't_1')
        self.assertEqual(self.bus.current_tool['status'], 'error')

    async def test_cancelled_ws_stream_skips_http_fallback(self):
        player = TTSPlayer(self.bus)
        player.synthesize = AsyncMock(return_value=b'fake-wav')
        stream = _FishStream(text='Never trigger HTTP after interrupt')
        entered = asyncio.Event()
        gate = asyncio.Event()

        class _SlowConnect:
            async def __aenter__(self):
                entered.set()
                await gate.wait()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        fake_ws = MagicMock(connect=MagicMock(return_value=_SlowConnect()))
        fake_msgpack = MagicMock()
        try:
            with patch.dict('sys.modules', {'websockets': fake_ws, 'msgpack': fake_msgpack}):
                task = asyncio.create_task(player._run_ws_stream(stream))
                stream.task = task
                await asyncio.wait_for(entered.wait(), timeout=2.0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            player.synthesize.assert_not_awaited()
            self.assertIsNone(stream.chunk_queue.get_nowait())
        finally:
            await player.close()
