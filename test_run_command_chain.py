import asyncio
import os
import uuid
from backend.console import _exec_vps

async def test_backup_and_delete_chain():
    print("🚀 Démarrage du test de chaînage run_command...")
    
    # 1. Configuration des chemins de test
    test_id = uuid.uuid4().hex[:6]
    test_file = f"/home/savant/test_to_delete_{test_id}.txt"
    backup_dir = "/home/savant/backups/hermes"
    backup_file = f"{backup_dir}/bak_test_{test_id}"
    
    print(f"📂 Fichier de test : {test_file}")
    
    # Préparation : Créer le fichier de test sur l'hôte
    # On utilise _exec_vps pour s'assurer que l'on passe par le même canal que l'agent
    await _exec_vps(f"echo 'Données importantes {test_id}' > {test_file}", "test-client")
    
    # 2. Construction de la chaîne && (Pattern exact du prompt Gemini)
    # [ -e target ] && mkdir -p backup_dir && cp -rp target backup_dest && rm -rf target && [ ! -e target ]
    command_chain = (
        f"[ -e {test_file} ] && "
        f"mkdir -p {backup_dir} && "
        f"cp -rp {test_file} {backup_file} && "
        f"rm -rf {test_file} && "
        f"[ ! -e {test_file} ]"
    )
    
    print(f"🔗 Exécution de la chaîne : \n   {command_chain}")
    
    # 3. Exécution via run_command (émulé par _exec_vps)
    result = await _exec_vps(command_chain, "test-client")
    
    # 4. Vérification des résultats
    print("\n📊 Résultats de l'exécution :")
    print(f"   Exit Code : {result.get('code')}")
    print(f"   Stdout    : {result.get('stdout', '').strip()}")
    print(f"   Stderr    : {result.get('stderr', '').strip()}")
    
    success = True
    
    # Vérification 1 : Code de sortie 0
    if result.get('code') != 0:
        print("❌ ÉCHEC : La chaîne de commande a renvoyé un code d'erreur.")
        success = False
        
    # Vérification 2 : Le fichier original a disparu
    check_gone = await _exec_vps(f"[ ! -e {test_file} ]", "test-client")
    if check_gone.get('code') != 0:
        print("❌ ÉCHEC : Le fichier original existe toujours.")
        success = False
    else:
        print("✅ SUCCÈS : Le fichier original a été supprimé.")

    # Vérification 3 : Le backup existe
    check_backup = await _exec_vps(f"[ -e {backup_file} ]", "test-client")
    if check_backup.get('code') != 0:
        print("❌ ÉCHEC : Le backup n'a pas été créé.")
        success = False
    else:
        print("✅ SUCCÈS : Le backup a été créé avec succès.")
        # Nettoyage du backup de test
        await _exec_vps(f"rm -rf {backup_file}", "test-client")

    if success:
        print("\n🎉 TEST RÉUSSI : Le chaînage run_command fonctionne parfaitement !")
    else:
        print("\n☢️ TEST ÉCHOUÉ : Vérifiez les logs ci-dessus.")

if __name__ == "__main__":
    try:
        asyncio.run(test_backup_and_delete_chain())
    except Exception as e:
        print(f"Erreur fatale lors du test : {e}")