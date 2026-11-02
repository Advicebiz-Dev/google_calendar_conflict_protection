# Google Calendar Conflict Protection

Módulo personalizado para Odoo 19 Enterprise.

## Funcionalidades

- Protege a sincronização entre o Google Calendar e o Odoo, dando prioridade às alterações do Google em situações de conflito.
- Verifica o estado atualizado das reuniões antes de abrir, editar, guardar, eliminar ou alterar horários.
- Identifica conflitos de sincronização e permite a sua resolução manual.
- Preserva as versões das reuniões em conflito, evitando a perda de informação.
- Deteta sobreposições de horários entre reuniões diferentes.
- Mantém a sincronização bidirecional, com atualização automática a cada 5 minutos.
- Protege reuniões recorrentes contra alterações indevidas.

## Gestão de convites e cancelamentos

- **Aceite:** a reunião aparece normalmente no Odoo.
- **Recusado:** a reunião é ocultada do calendário do convidado, sem ser eliminada.
- **Aceite novamente:** a reunião reaparece após sincronização, mantendo o registo original.
- **Provisório:** o estado de participação é atualizado.
- **Cancelado pelo organizador:** a reunião é arquivada no Odoo, sem enviar alterações ao Google.
- **Restaurado no Google:** a reunião arquivada pelo módulo é reativada, sem criar duplicados.

## Versão 19.0.1.7.0

- Corrige o erro `Invalid field 'gc_guard_remote_archived'`.
- Corrige a apresentação de convites recusados pelo próprio utilizador.
- Permite recuperar a apresentação de reuniões anteriormente recusadas e novamente aceites.
- Mantém a associação ao evento original através do `google_id`.
- Preserva as regras de conflitos e a frequência de sincronização.

## Limitações

- Não recupera automaticamente associações `google_id` perdidas em versões anteriores.
- Reuniões com conflitos antigos podem necessitar de correção manual.
- Os eventos recusados continuam armazenados na base de dados, embora ocultos no calendário do convidado.

## Atualização

1. Efetuar backup da base de dados.
2. Substituir a pasta `google_calendar_conflict_protection`.
3. Reiniciar o Odoo.
4. Atualizar o módulo.
5. Atualizar o navegador (`Ctrl + Shift + R`).
6. Testar o ciclo **Aceite → Recusado → Aceite**, os cancelamentos e a resolução de conflitos.

**Nota:** Versão candidata a testes em DEV. A instalação em produção deve ser precedida de validação em DEV e PRE, incluindo testes com convites externos, reuniões recorrentes e diferentes permissões de utilizador.
