// Copyright (c) 2026 RAD Debugger MCP contributors
// Licensed under the MIT license. Included only in the private MCP backend build.

#pragma comment(lib, "comdlg32")

global char rad_mcp_auth_token[65];
global B32 rad_mcp_eval_read_failed;

// Keep the UI's usual reads for registers and metadata, but do not use its
// asynchronous process-memory artifact cache for machine evaluation. That cache
// can return a full-sized zero buffer while its current-stop page is pending.
internal B32
rad_mcp_eval_space_read(E_Space space, void *out, E_SpaceRangeInfo *out_range_info, Rng1U64 range)
{
  B32 result = 0;
  D_Entity *entity = space.kind == D_EvalSpaceKind_Entity ? rd_ctrl_entity_from_eval_space(space) : &d_entity_nil;
  if(entity->kind == D_EntityKind_Process)
  {
    if(range.max >= range.min && !d_ctrl_targets_running())
    {
      U64 read_size = d_process_read(entity->handle, range, out);
      result = read_size == dim_1u64(range);
    }
  }
  else
  {
    result = rd_eval_space_read(space, out, out_range_info, range);
  }
  if(!result) { rad_mcp_eval_read_failed = 1; }
  return result;
}

internal void
rad_mcp_eval_invalidate_interpretations(void)
{
  // Leave parsed expressions, IR, and their type keys intact. Only computed
  // values may have observed the normal UI read hook earlier in this frame.
  for EachIndex(i, e_cache->key_slots_count)
  {
    for(E_CacheNode *node = e_cache->key_slots[i].first; node != 0; node = node->key_next)
    {
      node->bundle.flags &= ~E_CacheBundleFlag_Interpret;
    }
  }
}

// TCP is a byte stream. Upstream treats each WSARecv completion as a packet;
// keep a bounded per-peer accumulator for the sidecar's sequential requests.
typedef struct RAD_MCP_Recv RAD_MCP_Recv;
struct RAD_MCP_Recv
{
  SOCK_Endpoint endpoint;
  U64 size;
  U64 touched;
  U8 bytes[4096];
};
global RAD_MCP_Recv rad_mcp_recv[32];

internal String8
rad_mcp_receive_packet(Arena *arena, SOCK_Endpoint endpoint, String8 chunk)
{
  RAD_MCP_Recv *peer = 0;
  RAD_MCP_Recv *oldest = &rad_mcp_recv[0];
  for EachElement(i, rad_mcp_recv)
  {
    if(sock_endpoint_match(rad_mcp_recv[i].endpoint, endpoint)) { peer = &rad_mcp_recv[i]; break; }
    if(rad_mcp_recv[i].touched < oldest->touched) { oldest = &rad_mcp_recv[i]; }
  }
  if(peer == 0) { peer = oldest; MemoryZeroStruct(peer); peer->endpoint = endpoint; }
  peer->touched = now_time_us();
  if(chunk.size > sizeof(peer->bytes) - peer->size) { peer->size = 0; return str8_zero(); }
  MemoryCopy(peer->bytes + peer->size, chunk.str, chunk.size);
  peer->size += chunk.size;
  if(peer->size < 12) { return str8_zero(); }
  U32 magic = 0;
  U64 size = 0;
  MemoryCopy(&magic, peer->bytes, 4);
  MemoryCopy(&size, peer->bytes + 4, 8);
  if(magic != 0x72726300 || size > 3500) { peer->size = 0; return str8_zero(); }
  if(peer->size < size + 12) { return str8_zero(); }
  if(peer->size != size + 12) { peer->size = 0; return str8_zero(); }
  String8 result = str8_copy(arena, str8(peer->bytes, peer->size));
  peer->size = 0;
  return result;
}

internal B32
rad_mcp_init_auth(void)
{
  DWORD size = GetEnvironmentVariableA("RAD_MCP_TOKEN", rad_mcp_auth_token, sizeof(rad_mcp_auth_token));
  SetEnvironmentVariableA("RAD_MCP_TOKEN", 0);
  if(size != 64) { return 0; }
  for(U64 i = 0; i < 64; i += 1)
  {
    char c = rad_mcp_auth_token[i];
    if(!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) { return 0; }
  }
  return 1;
}

internal B32
rad_mcp_authenticate(String8 *message)
{
  U64 difference = 0;
  if(message->size < 65) { return 0; }
  for(U64 i = 0; i < 64; i += 1) { difference |= message->str[i] ^ (U8)rad_mcp_auth_token[i]; }
  difference |= message->str[64] ^ '\n';
  if(difference != 0) { return 0; }
  *message = str8_skip(*message, 65);
  return 1;
}

// The ordinary line lookup returns every enclosing inline location. Select the
// frame's own line table instead so its function name and location agree.
internal D_Line
rad_mcp_frame_line(Arena *arena, RDI_Parsed *rdi, RDI_LineTable *table, String8 dbgi_path, U64 voff)
{
  D_Line result = {0};
  RDI_ParsedLineTable parsed = {0};
  rdi_parsed_from_line_table(rdi, table, &parsed);
  U64 idx = rdi_line_info_idx_from_voff(&parsed, voff);
  if(idx < parsed.count)
  {
    RDI_Line *line = &parsed.lines[idx];
    RDI_SourceFile *file = rdi_element_from_name_idx(rdi, SourceFiles, line->file_idx);
    String8List path_parts = {0};
    for(RDI_FilePathNode *node = rdi_element_from_name_idx(rdi, FilePathNodes, file->file_path_node_idx);
        node != rdi_element_from_name_idx(rdi, FilePathNodes, 0);
        node = rdi_element_from_name_idx(rdi, FilePathNodes, node->parent_path_node))
    {
      String8 part = {0};
      part.str = rdi_string_from_idx(rdi, node->name_string_idx, &part.size);
      str8_list_push_front(arena, &path_parts, part);
    }
    String8 path = str8_list_join(arena, &path_parts, &(StringJoin){.sep = s("/")});
    if(file->file_path_node_idx != 0 && path.size != 0)
    {
      result.file_path = dbgi_path.size ? path_absolute_dst_from_relative_dst_src(arena, path, str8_chop_last_slash(dbgi_path)) : path;
    }
    result.pt = txt_pt(line->line_num, idx < parsed.col_count ? parsed.cols[idx].col_first : 1);
    result.voff_range = r1u64(parsed.voffs[idx], parsed.voffs[idx+1]);
  }
  return result;
}

internal B32
rad_mcp_configure_target(Arena *arena, String8 path)
{
  if(d_entity_array_from_kind(D_EntityKind_Process).count != 0) { return 0; }
  String8 data = data_from_file_path(arena, path);
  if(data.size == 0 || data.size > KB(64)) { return 0; }
  CFG_NodePtrList inputs = cfg_node_ptr_list_from_string(arena, rd_state->cfg, rd_state->cfg_schema_table, str8_chop_last_slash(path), data);
  if(inputs.count != 1 || !str8_match(inputs.first->v->string, s("target"), 0)) { return 0; }
  CFG_Node *project = cfg_node_child_from_string(cfg_node_root(), s("project"));
  CFG_NodePtrList old_targets = cfg_node_child_list_from_string(arena, project, s("target"));
  for EachNode(n, CFG_NodePtrNode, old_targets.first) { cfg_node_release(rd_state->cfg, n->v); }
  cfg_node_insert_child(rd_state->cfg, project, project->last, inputs.first->v);
  return 1;
}

internal void
rad_mcp_append_state(Arena *arena, String8List *parts)
{
  D_Entity *selected_thread = d_entity_from_handle(rd_base_regs()->thread);
  D_Entity *selected_process = d_entity_ancestor_from_kind(selected_thread, D_EntityKind_Process);
  str8_list_pushf(arena, parts, " mcp_bridge_version: 1\n");
  str8_list_pushf(arena, parts, " mcp_loopback_only: 1\n");
  str8_list_pushf(arena, parts, " mcp_authenticated: 1\n");
  str8_list_pushf(arena, parts, " selected_thread_id: %I64u\n", selected_thread->id);
  str8_list_pushf(arena, parts, " selected_process_id: %I64u\n", selected_process->id);
  str8_list_pushf(arena, parts, " selected_unwind_count: %I64u\n", rd_base_regs()->unwind_count);
  str8_list_pushf(arena, parts, " selected_inline_depth: %I64u\n", rd_base_regs()->inline_depth);
  str8_list_pushf(arena, parts, " processes:\n {\n");
  D_EntityArray processes = d_entity_array_from_kind(D_EntityKind_Process);
  for EachIndex(i, processes.count)
  {
    D_Entity *p = processes.v[i];
    str8_list_pushf(arena, parts, "  { id: %I64u name: \"%S\" is_dump: %u }\n",
                    p->id, escaped_from_raw_str8(arena, p->string), (U32)(p->handle.controller_kind == D_ControllerKind_Dump));
  }
  str8_list_pushf(arena, parts, " }\n");
  str8_list_pushf(arena, parts, " breakpoints:\n {\n");
  CFG_NodePtrList bps = cfg_node_top_level_list_from_string(arena, s("breakpoint"));
  for EachNode(n, CFG_NodePtrNode, bps.first)
  {
    if(rd_cfg_is_project_filtered(n->v)) { continue; }
    RD_Location loc = rd_location_from_cfg(n->v);
    String8 condition = cfg_node_child_from_string(n->v, s("condition"))->first->string;
    String8 hits = cfg_node_child_from_string(n->v, s("hit_count"))->first->string;
    U64 hit_count = 0;
    try_u64_from_str8_c_rules(hits, &hit_count);
    str8_list_pushf(arena, parts, "  { id: \"$%I64x\" file: \"%S\" line: %I64d expression: \"%S\" condition: \"%S\" enabled: %u hit_count: %I64u }\n",
                    n->v->id, escaped_from_raw_str8(arena, loc.file_path), loc.pt.line,
                    escaped_from_raw_str8(arena, loc.expr), escaped_from_raw_str8(arena, condition),
                    (U32)!rd_disabled_from_cfg(n->v), hit_count);
  }
  str8_list_pushf(arena, parts, " }\n");
  str8_list_pushf(arena, parts, " targets:\n {\n");
  CFG_NodePtrList targets = cfg_node_top_level_list_from_string(arena, s("target"));
  for EachNode(n, CFG_NodePtrNode, targets.first)
  {
    if(rd_cfg_is_project_filtered(n->v)) { continue; }
    String8 exe = cfg_node_child_from_string(n->v, s("executable"))->first->string;
    str8_list_pushf(arena, parts, "  { executable: \"%S\" enabled: %u }\n", escaped_from_raw_str8(arena, exe), (U32)!rd_disabled_from_cfg(n->v));
  }
  str8_list_pushf(arena, parts, " }\n");
  Access *access = access_open();
  D_CallStack stack = {0};
  if(!d_ctrl_targets_running() && selected_thread->kind == D_EntityKind_Thread)
  {
    stack = d_call_stack_from_thread(access, selected_thread->handle, 1, now_time_us()+50000);
  }
  U64 count = Min(stack.frames_count, 128);
  str8_list_pushf(arena, parts, " stack_total_count: %I64u\n stack_truncated: %u\n stack:\n {\n", stack.frames_count, (U32)(count < stack.frames_count));
  ARCH_Info *arch_info = arch_info_from_arch(selected_thread->arch);
  for EachIndex(i, count)
  {
    D_CallStackFrame *f = &stack.frames[i];
    U64 address = arch_ip_from_reg_block(arch_info, f->regs);
    D_Entity *module = d_module_from_process_vaddr(selected_process, address);
    U64 voff = d_voff_from_vaddr(module, address);
    DI_Key key = d_dbgi_key_from_module(module);
    RDI_Parsed *rdi = di_rdi_from_key(access, key, 0, 0);
    RDI_Symbol *procedure = rdi_procedure_from_voff(rdi, voff);
    String8 symbol = fully_qualified_str8_from_rdi_symbol(arena, rdi, procedure);
    RDI_LineTable *line_table = rdi_line_table_from_unit(rdi, rdi_unit_from_voff(rdi, voff));
    if(f->inline_depth != 0)
    {
      RDI_Scope *scope = rdi_scope_from_voff(rdi, voff);
      U64 inline_count = 0;
      for(RDI_Scope *s = scope; s->inline_site_idx != 0; s = rdi_element_from_name_idx(rdi, Scopes, s->parent_scope_idx))
      {
        inline_count += 1;
      }
      RDI_InlineSite *site = rdi_element_from_name_idx(rdi, InlineSites, 0);
      for(RDI_Scope *s = scope; s->inline_site_idx != 0; s = rdi_element_from_name_idx(rdi, Scopes, s->parent_scope_idx))
      {
        if(inline_count == f->inline_depth)
        {
          site = rdi_element_from_name_idx(rdi, InlineSites, s->inline_site_idx);
          break;
        }
        inline_count -= 1;
      }
      symbol.str = rdi_string_from_idx(rdi, site->name_string_idx, &symbol.size);
      line_table = rdi_element_from_name_idx(rdi, LineTables, site->line_table_idx);
    }
    D_Entity *dbg_path = d_entity_child_from_kind(module, D_EntityKind_DebugInfoPath);
    D_Line line = rad_mcp_frame_line(arena, rdi, line_table, dbg_path->string, voff);
    str8_list_pushf(arena, parts, "  { index: %I64u unwind_count: %I64u inline_depth: %I64u address: 0x%I64x module: \"%S\" symbol: \"%S\" file: \"%S\" line: %I64d }\n",
                    i, f->unwind_count, f->inline_depth, address, escaped_from_raw_str8(arena, module->string),
                    escaped_from_raw_str8(arena, symbol), escaped_from_raw_str8(arena, line.file_path), line.pt.line);
  }
  str8_list_pushf(arena, parts, " }\n");
  access_close(access);
}
