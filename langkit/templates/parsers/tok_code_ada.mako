## vim: filetype=makoada

--  Start tok_code

## Get the current token
${res} := ${pos_name};

declare
   T : constant Token_Data_Type :=
      Token_Vectors.Get (Parser.TDH.Tokens, Natural (${res}));
begin
   if
      T.Kind /= ${token_kind}
      % if match_text:
      or else T.Text.all /= "${match_text}"
      % endif
   then
       ## If the result is not the one we expect, set pos to error
       ${pos} := No_Token_Index;

       ## Document this failure so we can have a diagnostic at the end of
       ## parsing.
       if Parser.Last_Fail.Pos <= ${pos_name} then
           Parser.Last_Fail.Pos := ${pos_name};
           Parser.Last_Fail.Expected_Token_Id := ${token_kind};
           Parser.Last_Fail.Found_Token_Id := T.Kind;
       end if;
   else
      ## We don't want to increment the position if we are matching the
      ## termination token (eg. the last token in the token stream).
      % if token_kind == ctx.lexer.ada_token_name('termination'):
          ${pos} := ${pos_name};
      ## Else increment the current position
      % else:
          ${pos} := ${pos_name} + 1;
      % endif
   end if;
end;

--  End tok_code
